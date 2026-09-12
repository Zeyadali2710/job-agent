"""Phase 13's dry-run deliverable, stated as a property rather than a claim.

Approval means "this may be sent". Sending is Phase 14 and is guarded three
independent ways -- the DRY_RUN environment variable, an assertion inside the
submit function, and a database trigger on application_attempts. The brief is
explicit that none of those may be bypassed, so these tests check that the
approval path structurally cannot reach any of them.
"""
from pathlib import Path

from job_agent.application.transitions import ALLOWED, can

SRC = Path(__file__).resolve().parents[2] / "src" / "job_agent"
APPROVAL_PATH = [
    SRC / "web" / "routes.py",
    SRC / "application" / "route.py",
    SRC / "application" / "rules.py",
    SRC / "application" / "transitions.py",
]


def test_nothing_on_the_approval_path_touches_application_attempts():
    """application_attempts is the table the DRY_RUN trigger guards, and the
    SUBMIT_INTENT row is committed before the click. Nothing that approves an
    application may write one -- if approval could, the trigger would be the
    only thing standing between a dashboard click and a live submission."""
    offenders = [f.name for f in APPROVAL_PATH
                 if "application_attempts" in f.read_text(encoding="utf-8")]
    assert not offenders, f"approval path writes attempts: {offenders}"


def test_the_approval_path_contains_no_http_client():
    """A submission is an outbound request. The approval path makes none."""
    banned = ("httpx", "requests", "aiohttp", "urllib.request")
    offenders = []
    for f in APPROVAL_PATH:
        body = f.read_text(encoding="utf-8")
        offenders += [f"{f.name}: {b}" for b in banned if b in body]
    assert not offenders, offenders


def test_approval_cannot_reach_a_sent_state_in_one_step():
    """APPROVED -> SUBMITTED would skip SUBMITTING, which is where Phase 14's
    guarded attempt lifecycle lives. The only way out of APPROVED toward
    sending is through it."""
    assert not can("APPROVED", "SUBMITTED")
    assert not can("APPROVED", "CONFIRMED")
    assert not can("AWAITING_APPROVAL", "SUBMITTING")
    assert can("APPROVED", "SUBMITTING")


def test_a_withdrawn_application_can_never_come_back():
    """Rejecting is final for that row. The partial unique index frees the slot
    so a deliberate re-application is a new application, not a resurrected one
    carrying the old one's history."""
    assert ALLOWED["WITHDRAWN"] == frozenset()


SENDING_STATES = {"SUBMITTING", "SUBMITTED", "CONFIRMED"}


def test_the_dashboard_can_only_target_three_non_sending_statuses():
    """Read the handler's decision map out of the source rather than grepping
    for the word "submit" -- the first version of this test failed on its own
    docstring, which is a good reminder that a grep is not a property.
    """
    import ast
    tree = ast.parse((SRC / "web" / "routes.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "application_decision")
    targets = {c.value for d in ast.walk(fn) if isinstance(d, ast.Dict)
               for c in d.values if isinstance(c, ast.Constant)}
    assert targets == {"APPROVED", "WITHDRAWN", "AWAITING_APPROVAL"}
    assert not targets & SENDING_STATES
