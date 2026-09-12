"""The routing rule engine. Pure functions: rules in, decision out.

Architecture 0.3 sets the posture this encodes. Mode 2 -- stop at the submit
button and wait for a person -- is the default, and mode 3 is per-company
allowlisted and off. The engine cannot produce an unattended submission unless
a rule explicitly names the employer, because `application_rules` carries a
CHECK that AUTO_APPLY without an allowlist is not a row the database will hold.

No DB, no clock, no I/O, so the whole decision matrix is testable directly.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

AUTO_APPLY = "AUTO_APPLY"
REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
SKIP = "SKIP"

MODE_DRAFT_ONLY = 1
MODE_APPROVAL = 2
MODE_AUTO = 3

# The status each action routes to. AUTO_APPLY reaches APPROVED without a
# person; everything else stops somewhere a person has to act.
STATUS_FOR = {
    AUTO_APPLY: "APPROVED",
    REQUIRE_APPROVAL: "AWAITING_APPROVAL",
    SKIP: "WITHDRAWN",
}
MODE_FOR = {
    AUTO_APPLY: MODE_AUTO,
    REQUIRE_APPROVAL: MODE_APPROVAL,
    SKIP: MODE_DRAFT_ONLY,
}


@dataclass(frozen=True)
class Decision:
    action: str
    status: str
    mode: int
    rule: str
    reason: str


def _as_set(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.lower()}
    return {str(v).lower() for v in value}


def matches(match: Mapping, job: Mapping) -> tuple[bool, str]:
    """Does one rule's `match` object describe this job?

    Every key is a conjunct: an empty match object matches everything, which is
    what makes the seeded catch-all rule work. An unknown key fails closed --
    a typo in a rule must not silently widen it into a catch-all that
    auto-applies.
    """
    for key, want in (match or {}).items():
        if key == "min_score":
            if float(job.get("score") or 0) < float(want):
                return False, f"score {job.get('score')} < min_score {want}"
        elif key == "max_score":
            if float(job.get("score") or 0) > float(want):
                return False, f"score {job.get('score')} > max_score {want}"
        elif key == "company_slug_in":
            if str(job.get("company_slug") or "").lower() not in _as_set(want):
                return False, f"company {job.get('company_slug')!r} not in list"
        elif key == "company_slug_not_in":
            if str(job.get("company_slug") or "").lower() in _as_set(want):
                return False, f"company {job.get('company_slug')!r} is excluded"
        elif key == "location_class_in":
            if str(job.get("location_class") or "").lower() not in _as_set(want):
                return False, f"location {job.get('location_class')!r} not in list"
        elif key == "is_grad_program":
            if bool(job.get("is_grad_program")) is not bool(want):
                return False, f"is_grad_program is {bool(job.get('is_grad_program'))}"
        elif key == "title_matches":
            if not re.search(str(want), str(job.get("title") or ""), re.I):
                return False, f"title does not match {want!r}"
        elif key == "title_not_matches":
            if re.search(str(want), str(job.get("title") or ""), re.I):
                return False, f"title matches excluded {want!r}"
        else:
            # Fail closed. A rule nobody can evaluate is a rule nobody should
            # act on, and the alternative is a misspelt key quietly turning a
            # narrow rule into a catch-all.
            return False, f"unknown match key {key!r}"
    return True, "matched"


def decide(rules: Sequence[Mapping], job: Mapping, *,
           auto_apply_allowed: bool = True) -> Decision:
    """First enabled rule, by ascending priority, whose match is satisfied.

    `auto_apply_allowed` is the daily cap and any other external brake. When it
    is false an AUTO_APPLY rule degrades to approval rather than being skipped:
    the job still deserves an application, it just does not get an unattended
    one today.
    """
    ordered = sorted((r for r in rules if r.get("enabled", True)),
                     key=lambda r: (int(r.get("priority", 100)), str(r.get("name", ""))))
    for rule in ordered:
        ok, why = matches(rule.get("match") or {}, job)
        if not ok:
            continue
        action = str(rule.get("action") or REQUIRE_APPROVAL)
        name = str(rule.get("name") or "unnamed")

        if action == AUTO_APPLY:
            # Empty entries are dropped and an unresolved employer is never a
            # match: otherwise an allowlist of [""] and a job with no company
            # both normalise to "" and every unattributed job auto-applies.
            allowlist = {c for c in _as_set(rule.get("company_allowlist")) if c}
            company = str(job.get("company_slug") or "").lower()
            if not company or not allowlist or company not in allowlist:
                return Decision(
                    REQUIRE_APPROVAL, STATUS_FOR[REQUIRE_APPROVAL],
                    MODE_FOR[REQUIRE_APPROVAL], name,
                    f"rule {name!r} auto-applies but {company or 'this company'} "
                    f"is not on its allowlist")
            if not auto_apply_allowed:
                return Decision(
                    REQUIRE_APPROVAL, STATUS_FOR[REQUIRE_APPROVAL],
                    MODE_FOR[REQUIRE_APPROVAL], name,
                    f"rule {name!r} auto-applies but the daily cap is reached")

        return Decision(action, STATUS_FOR.get(action, "AWAITING_APPROVAL"),
                        MODE_FOR.get(action, MODE_APPROVAL), name,
                        f"rule {name!r}: {why}")

    # No rule matched. Approval, not auto-apply and not silence: an
    # unclassified job is one a person should look at.
    return Decision(REQUIRE_APPROVAL, STATUS_FOR[REQUIRE_APPROVAL],
                    MODE_APPROVAL, "none", "no rule matched; defaulting to approval")


# ---------------------------------------------------------------- readiness

BLOCK_QUARANTINED = "cover letter failed provenance validation"
BLOCK_NO_DOCUMENTS = "documents have not been generated"
BLOCK_NOT_RENDERED = "documents have not been rendered to PDF"
BLOCK_OPEN_QUESTIONS = "required questions are unanswered"


def readiness(docs: Sequence[Mapping], open_required: int) -> list[str]:
    """Why this application is not ready to route. Empty list means ready.

    Architecture 9: "the application cannot be submitted while any required
    question is unanswered." That is enforced here rather than at the submit
    button, so an application never reaches a state that implies it is good to
    go while something is missing.
    """
    blockers: list[str] = []
    kinds = {str(d.get("kind")) for d in docs}
    if not {"RESUME", "COVER_LETTER"} <= kinds:
        blockers.append(BLOCK_NO_DOCUMENTS)
    if any(d.get("validator_state") == "FABRICATION_SUSPECTED" for d in docs):
        blockers.append(BLOCK_QUARANTINED)
    if docs and any(not d.get("path") for d in docs
                    if d.get("validator_state") == "PASSED"):
        blockers.append(BLOCK_NOT_RENDERED)
    if open_required > 0:
        blockers.append(f"{BLOCK_OPEN_QUESTIONS} ({open_required})")
    return blockers
