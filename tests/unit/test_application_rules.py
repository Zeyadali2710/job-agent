"""The routing rule engine and the transition table. Pure, no DB.

Architecture 0.3: mode 2 is the default and mode 3 is per-company allowlisted
and off. These tests exist to make sure no combination of rules produces an
unattended submission that a person did not explicitly enable.
"""
import pytest

from job_agent.application import rules as R
from job_agent.application.transitions import ALLOWED, IllegalTransition, can, check

JOB = {"score": 72.0, "title": "2026 Graduate Software Engineer",
       "company_slug": "atlassian", "location_class": "SYDNEY_CBD",
       "is_grad_program": True}

CATCH_ALL = {"name": "default-require-approval", "enabled": True,
             "priority": 1000, "match": {}, "action": R.REQUIRE_APPROVAL,
             "company_allowlist": []}


# ------------------------------------------------------------------ matching

@pytest.mark.parametrize("match,expected", [
    ({}, True),                                        # the seeded catch-all
    ({"min_score": 70}, True),
    ({"min_score": 90}, False),
    ({"max_score": 60}, False),
    ({"company_slug_in": ["atlassian", "canva"]}, True),
    ({"company_slug_in": ["canva"]}, False),
    ({"company_slug_not_in": ["atlassian"]}, False),
    ({"location_class_in": ["SYDNEY_CBD"]}, True),
    ({"is_grad_program": True}, True),
    ({"is_grad_program": False}, False),
    ({"title_matches": "graduate|intern"}, True),
    ({"title_matches": "principal"}, False),
    ({"title_not_matches": "principal"}, True),
    ({"min_score": 70, "is_grad_program": True}, True),   # conjunction
    ({"min_score": 70, "is_grad_program": False}, False),
])
def test_match_conditions(match, expected):
    assert R.matches(match, JOB)[0] is expected


def test_an_unknown_match_key_fails_closed():
    """A typo must narrow a rule to nothing, never widen it. `{"min_scor": 90}`
    silently matching everything would turn a careful rule into a catch-all."""
    ok, why = R.matches({"min_scor": 90}, JOB)
    assert ok is False and "unknown match key" in why


# ------------------------------------------------------------------ deciding

def test_the_seeded_catch_all_sends_everything_to_approval():
    d = R.decide([CATCH_ALL], JOB)
    assert d.action == R.REQUIRE_APPROVAL
    assert d.status == "AWAITING_APPROVAL" and d.mode == R.MODE_APPROVAL


def test_no_rules_at_all_still_means_approval_not_silence():
    """An unclassified job is one a person should look at. Defaulting to SKIP
    would lose it; defaulting to AUTO_APPLY would be unforgivable."""
    d = R.decide([], JOB)
    assert d.status == "AWAITING_APPROVAL" and d.rule == "none"


def test_lower_priority_number_wins():
    specific = {"name": "skip-recruiters", "priority": 10,
                "match": {"company_slug_in": ["atlassian"]}, "action": R.SKIP,
                "company_allowlist": []}
    d = R.decide([CATCH_ALL, specific], JOB)
    assert d.action == R.SKIP and d.rule == "skip-recruiters"


def test_a_disabled_rule_is_not_consulted():
    disabled = {"name": "off", "enabled": False, "priority": 1,
                "match": {}, "action": R.SKIP, "company_allowlist": []}
    assert R.decide([disabled, CATCH_ALL], JOB).action == R.REQUIRE_APPROVAL


def test_skip_withdraws_rather_than_leaving_it_hanging():
    skip = {"name": "no-consultancies", "priority": 10, "match": {},
            "action": R.SKIP, "company_allowlist": []}
    d = R.decide([skip], JOB)
    assert d.status == "WITHDRAWN" and d.mode == R.MODE_DRAFT_ONLY


# --------------------------------------------------- auto-apply is gated hard

def test_auto_apply_requires_the_company_to_be_on_its_own_allowlist():
    """The database CHECK already refuses an AUTO_APPLY rule with an empty
    allowlist. This is the other half: a rule may match a job at a company its
    allowlist does not name, and that must not auto-apply."""
    rule = {"name": "auto-atlassian", "priority": 10, "match": {},
            "action": R.AUTO_APPLY, "company_allowlist": ["canva"]}
    d = R.decide([rule], JOB)
    assert d.action == R.REQUIRE_APPROVAL and "not on its allowlist" in d.reason


def test_auto_apply_works_when_the_company_is_named():
    rule = {"name": "auto-atlassian", "priority": 10, "match": {},
            "action": R.AUTO_APPLY, "company_allowlist": ["atlassian"]}
    d = R.decide([rule], JOB)
    assert d.action == R.AUTO_APPLY and d.status == "APPROVED"
    assert d.mode == R.MODE_AUTO


def test_the_daily_cap_downgrades_auto_apply_to_approval_rather_than_skipping():
    """The job still deserves an application; it just does not get an
    unattended one today. Skipping would lose it silently."""
    rule = {"name": "auto-atlassian", "priority": 10, "match": {},
            "action": R.AUTO_APPLY, "company_allowlist": ["atlassian"]}
    d = R.decide([rule], JOB, auto_apply_allowed=False)
    assert d.status == "AWAITING_APPROVAL" and "daily cap" in d.reason


def test_a_missing_company_can_never_auto_apply():
    """A job whose employer was never resolved has no slug, so it cannot be on
    any allowlist -- and an empty allowlist entry must not match an empty slug."""
    rule = {"name": "auto", "priority": 10, "match": {},
            "action": R.AUTO_APPLY, "company_allowlist": [""]}
    d = R.decide([rule], {**JOB, "company_slug": None})
    assert d.action == R.REQUIRE_APPROVAL


# ------------------------------------------------------------------ readiness

def READY_DOCS():
    return [{"kind": "RESUME", "validator_state": "PASSED", "path": "/a.pdf"},
            {"kind": "COVER_LETTER", "validator_state": "PASSED", "path": "/b.pdf"}]


def test_an_application_with_everything_is_ready():
    assert R.readiness(READY_DOCS(), 0) == []


def test_a_quarantined_cover_letter_holds_the_application():
    docs = READY_DOCS()
    docs[1] = {"kind": "COVER_LETTER", "validator_state": "FABRICATION_SUSPECTED",
               "path": None}
    assert R.BLOCK_QUARANTINED in R.readiness(docs, 0)


def test_an_unanswered_required_question_holds_the_application():
    """Architecture 9: "the application cannot be submitted while any required
    question is unanswered." Enforced here rather than at the submit button, so
    it never reaches a state implying it is good to go."""
    blockers = R.readiness(READY_DOCS(), 2)
    assert any(R.BLOCK_OPEN_QUESTIONS in b for b in blockers)


def test_missing_documents_hold_the_application():
    assert R.BLOCK_NO_DOCUMENTS in R.readiness([], 0)
    assert R.BLOCK_NO_DOCUMENTS in R.readiness(
        [{"kind": "RESUME", "validator_state": "PASSED", "path": "/a.pdf"}], 0)


def test_a_passed_document_that_never_rendered_holds_the_application():
    docs = READY_DOCS()
    docs[0]["path"] = None
    assert R.BLOCK_NOT_RENDERED in R.readiness(docs, 0)


# ------------------------------------------------------------------ transitions

def test_the_happy_path_is_legal_end_to_end():
    path = ["DRAFT", "READY", "AWAITING_APPROVAL", "APPROVED", "SUBMITTING",
            "SUBMITTED", "CONFIRMED"]
    for a, b in zip(path, path[1:]):
        check(a, b)


@pytest.mark.parametrize("frm,to", [
    ("WITHDRAWN", "APPROVED"),      # a withdrawn application must stay dead
    ("CONFIRMED", "SUBMITTING"),    # nothing follows confirmed
    ("DRAFT", "SUBMITTED"),         # skipping the entire approval workflow
    ("DRAFT", "APPROVED"),          # approving something never validated
    ("AWAITING_APPROVAL", "SUBMITTED"),
])
def test_illegal_transitions_raise(frm, to):
    with pytest.raises(IllegalTransition):
        check(frm, to)


def test_approval_is_revocable_until_something_is_actually_sent():
    assert can("APPROVED", "AWAITING_APPROVAL") and can("APPROVED", "WITHDRAWN")
    assert not can("SUBMITTED", "WITHDRAWN")


def test_every_status_the_check_constraint_allows_appears_in_the_table():
    """If the schema gains a status and this table does not, every transition
    into it raises "unknown status" at runtime instead of at review."""
    from_schema = {
        "DRAFT", "READY", "AWAITING_APPROVAL", "APPROVED", "SUBMITTING",
        "SUBMITTED", "CONFIRMED", "VERIFY_REQUIRED", "WITHDRAWN",
        "FAILED_PERMANENT"}
    assert set(ALLOWED) == from_schema


def test_every_target_in_the_table_is_itself_a_known_status():
    for frm, targets in ALLOWED.items():
        for t in targets:
            assert t in ALLOWED, f"{frm} -> {t} is not a declared status"
