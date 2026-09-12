"""The attempt lifecycle, the DRY_RUN guard, and the mock adapter.

Architecture 10's dangerous case is the one these exist for: a crash between
the click and the confirmation. The rule is that an attempt at or past
SUBMIT_INTENT is never auto-retried, and these tests make that a property
rather than a comment.
"""
import tempfile
from pathlib import Path

import pytest

from job_agent.application import attempts as A
from job_agent.application.adapters.base import (
    Document, SubmitContext, form_hash,
)
from job_agent.application.adapters.mock import MockAdapter
from job_agent.application.engine import DryRunViolation, assert_submission_allowed

DOCS = [Document("RESUME", "/a.pdf", "a" * 64),
        Document("COVER_LETTER", "/b.pdf", "b" * 64)]


def ctx(dry_run=True, **over):
    base = dict(application_id=5, job_title="Graduate Engineer", company="Acme",
                job_url="https://example.test/j/1", documents=DOCS,
                answers={"why": "because"}, dry_run=dry_run)
    return SubmitContext(**{**base, **over})


# ----------------------------------------------------------------- the guard

def test_the_in_function_guard_refuses_while_dry_run_is_set():
    with pytest.raises(DryRunViolation):
        assert_submission_allowed(True, 5)


def test_the_guard_permits_a_live_submission_when_dry_run_is_off():
    assert_submission_allowed(False, 5) is None


def test_the_guard_is_not_an_assert_statement():
    """python -O strips assert statements. A safety property that disappears
    under an optimisation flag is not a safety property."""
    src = (Path(__file__).resolve().parents[2] / "src" / "job_agent"
           / "application" / "engine.py").read_text(encoding="utf-8")
    fn = src[src.index("def assert_submission_allowed"):]
    fn = fn[:fn.index("\n\n\n")]
    assert "raise DryRunViolation" in fn
    assert "assert " not in fn


# ------------------------------------------------------- the attempt lifecycle

def test_the_happy_path_is_legal_end_to_end():
    path = [A.PREPARED, A.NAVIGATING, A.FILLING, A.REVIEW_PENDING,
            A.SUBMIT_INTENT, A.SUBMITTED, A.CONFIRMED]
    for a, b in zip(path, path[1:]):
        A.check(a, b)


def test_only_states_before_submit_intent_are_resumable():
    """Architecture 10: "resume is only permitted from states before
    SUBMIT_INTENT". Everything before it is reading a page and filling boxes,
    which costs nothing twice. After it, a retry is how one application becomes
    two in a recruiter's inbox."""
    assert A.RESUMABLE == {A.PREPARED, A.NAVIGATING, A.FILLING, A.REVIEW_PENDING}
    for state in (A.SUBMIT_INTENT, A.SUBMITTED, A.CONFIRMED, A.VERIFY_REQUIRED):
        assert not A.is_resumable(state), state


def test_an_interrupted_attempt_past_the_click_becomes_a_question_not_a_retry():
    assert A.resolve_orphan(A.SUBMIT_INTENT) == A.VERIFY_REQUIRED
    assert A.resolve_orphan(A.SUBMITTED) == A.VERIFY_REQUIRED


def test_an_interrupted_attempt_before_the_click_needs_no_resolution():
    for state in A.RESUMABLE:
        assert A.resolve_orphan(state) is None


@pytest.mark.parametrize("frm,to", [
    (A.SUBMIT_INTENT, A.FILLING),        # rewinding past the point of no return
    (A.SUBMITTED, A.SUBMIT_INTENT),      # submitting twice
    (A.CONFIRMED, A.SUBMITTED),          # nothing follows confirmed
    (A.FAILED, A.PREPARED),              # a failed attempt is not reused
    (A.PREPARED, A.SUBMIT_INTENT),       # skipping the whole preparation
])
def test_illegal_attempt_transitions_raise(frm, to):
    with pytest.raises(A.IllegalAttemptTransition):
        A.check(frm, to)


def test_every_state_the_check_constraint_allows_is_in_the_table():
    from_schema = {"PREPARED", "NAVIGATING", "FILLING", "REVIEW_PENDING",
                   "SUBMIT_INTENT", "SUBMITTED", "CONFIRMED", "VERIFY_REQUIRED",
                   "FAILED", "ABANDONED"}
    assert set(A.ALLOWED) == from_schema


# ------------------------------------------------------------- the mock adapter

def test_the_mock_refuses_to_claim_a_submission_under_dry_run():
    """Redundant with the engine's guard and the database trigger, and here
    anyway: a mock that lied about submitting would make every test of the
    layers above it meaningless."""
    with tempfile.TemporaryDirectory() as d:
        a = MockAdapter(d)
        c = ctx(dry_run=True)
        result = a.submit(c, a.prepare(c))
    assert result.submitted is False and "DRY_RUN" in result.error


def test_the_mock_submits_when_dry_run_is_off():
    with tempfile.TemporaryDirectory() as d:
        a = MockAdapter(d)
        c = ctx(dry_run=False)
        result = a.submit(c, a.prepare(c))
    assert result.submitted and result.confirmation_ref.startswith("MOCK-")


def test_preparing_twice_produces_the_same_form_hash():
    """The hash is what makes a duplicate detectable after a crash: a
    SUBMIT_INTENT row can be compared against a fresh preparation. A hash that
    moved every run would make that comparison meaningless."""
    with tempfile.TemporaryDirectory() as d:
        a = MockAdapter(d)
        c = ctx()
        assert a.prepare(c).form_hash == a.prepare(c).form_hash


def test_the_form_hash_changes_when_a_document_changes():
    other = [Document("RESUME", "/a.pdf", "c" * 64), DOCS[1]]
    assert form_hash({"x": "1"}, DOCS) != form_hash({"x": "1"}, other)


def test_the_form_hash_ignores_field_ordering():
    assert form_hash({"a": "1", "b": "2"}, DOCS) == form_hash({"b": "2", "a": "1"}, DOCS)


def test_preparing_writes_a_snapshot_artifact():
    with tempfile.TemporaryDirectory() as d:
        prepared = MockAdapter(d).prepare(ctx())
        assert [x.kind for x in prepared.artifacts] == ["HTML_SNAPSHOT"]
        assert Path(prepared.artifacts[0].path).exists()


def test_the_applier_claims_only_its_own_kinds():
    from job_agent.workers import applier, loop, renderer
    assert set(applier.HANDLERS) == {"submit", "resolve_orphans"}
    assert not set(applier.HANDLERS) & set(loop.HANDLERS)
    assert not set(applier.HANDLERS) & set(renderer.HANDLERS)


def test_no_other_worker_can_claim_a_submit():
    """ja_worker and ja_renderer have no INSERT on application_attempts, so a
    submit item they claimed would fail all eight attempts."""
    from job_agent.workers import loop, renderer
    assert "submit" not in loop.HANDLERS and "submit" not in renderer.HANDLERS
