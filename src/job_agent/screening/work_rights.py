from __future__ import annotations

import re

MUST_CITIZEN = re.compile(
    r"\b(must be an? australian citizen|australian citizenship (is )?(required|mandatory)|"
    r"citizens? only|restricted to australian citizens)\b", re.I)
MUST_PR = re.compile(r"\b(must hold (permanent residency|pr)|permanent residents? only)\b", re.I)
NO_SPONSOR = re.compile(
    r"\b(no sponsorship|sponsorship (is )?not (available|offered|provided)|"
    r"unable to sponsor|we do not sponsor|no visa holders)\b", re.I)
SPONSOR_OK = re.compile(
    r"\b(sponsorship (is )?available|will sponsor|482 (visa )?available|"
    r"visa sponsorship (offered|available))\b", re.I)
FULL_RIGHTS = re.compile(
    r"\b(full working rights|unrestricted working rights|valid working rights|"
    r"full work rights|permanent working rights)\b", re.I)
PREFERRED = re.compile(r"\b(citizens? or (permanent residents?|pr) preferred|pr preferred)\b", re.I)

PROFILE_MEETS_CITIZEN = {"CITIZEN"}
PROFILE_MEETS_PR = {"CITIZEN", "PR"}
PROFILE_HAS_RIGHTS = {"CITIZEN", "PR", "TEMP_GRADUATE_485", "STUDENT_500"}


def classify(text: str | None, profile_status: str) -> tuple[str, str | None]:
    """Return (classification, clause_verbatim)."""
    if not text:
        return "UNCERTAIN", None

    def clause(m) -> str:
        s = max(0, m.start() - 100)
        return text[s:m.end() + 100].strip()

    if m := MUST_CITIZEN.search(text):
        if profile_status not in PROFILE_MEETS_CITIZEN:
            return "INCOMPATIBLE", clause(m)
        return "COMPATIBLE", clause(m)

    if m := MUST_PR.search(text):
        if profile_status not in PROFILE_MEETS_PR:
            return "INCOMPATIBLE", clause(m)
        return "COMPATIBLE", clause(m)

    if m := NO_SPONSOR.search(text):
        if profile_status == "REQUIRES_SPONSORSHIP":
            return "INCOMPATIBLE", clause(m)
        return "COMPATIBLE", clause(m)

    if m := SPONSOR_OK.search(text):
        return "SPONSORSHIP_POSSIBLE", clause(m)

    if m := PREFERRED.search(text):
        return "LIKELY_COMPATIBLE", clause(m)

    if m := FULL_RIGHTS.search(text):
        if profile_status in PROFILE_HAS_RIGHTS:
            return "COMPATIBLE", clause(m)
        return "INCOMPATIBLE", clause(m)

    return "UNCERTAIN", None
