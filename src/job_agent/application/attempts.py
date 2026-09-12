"""The attempt lifecycle. Architecture 10's failure-semantics table, as code.

    PREPARED -> NAVIGATING -> FILLING -> REVIEW_PENDING -> SUBMIT_INTENT
             -> SUBMITTED -> CONFIRMED

The important line in that table is not the happy path:

    "Any crash leaves the last committed state; resume is only permitted from
     states before SUBMIT_INTENT."

    "On restart, SUBMIT_INTENT without SUBMITTED is never auto-retried -- it
     becomes VERIFY_REQUIRED."

Everything before SUBMIT_INTENT is reading a page and filling boxes, and doing
it twice costs nothing. SUBMIT_INTENT means the click may already have landed,
and a retry from there is how one application becomes two in a recruiter's
inbox. So the resumable set is a property with a test, not a convention.

Pure: no DB, no I/O.
"""
from __future__ import annotations

PREPARED = "PREPARED"
NAVIGATING = "NAVIGATING"
FILLING = "FILLING"
REVIEW_PENDING = "REVIEW_PENDING"
SUBMIT_INTENT = "SUBMIT_INTENT"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
VERIFY_REQUIRED = "VERIFY_REQUIRED"
FAILED = "FAILED"
ABANDONED = "ABANDONED"

ALLOWED: dict[str, frozenset[str]] = {
    PREPARED: frozenset({NAVIGATING, FAILED, ABANDONED}),
    NAVIGATING: frozenset({FILLING, FAILED, ABANDONED}),
    FILLING: frozenset({REVIEW_PENDING, FAILED, ABANDONED}),
    REVIEW_PENDING: frozenset({SUBMIT_INTENT, FAILED, ABANDONED}),
    # Past this line a click may have happened. FAILED and ABANDONED are
    # reachable only for an outcome we positively observed -- a rehearsal under
    # DRY_RUN knows nothing was sent, so it abandons rather than leaving a
    # question behind. An *unobserved* interruption resolves to VERIFY_REQUIRED
    # instead, which is a question rather than a conclusion, and resolve_orphan
    # below is the only thing that decides that.
    SUBMIT_INTENT: frozenset({SUBMITTED, VERIFY_REQUIRED, FAILED, ABANDONED}),
    SUBMITTED: frozenset({CONFIRMED, VERIFY_REQUIRED, FAILED}),
    VERIFY_REQUIRED: frozenset({CONFIRMED, SUBMITTED, ABANDONED}),
    CONFIRMED: frozenset(),
    FAILED: frozenset(),
    ABANDONED: frozenset(),
}

# The states a crashed attempt may be picked back up from. Deliberately every
# state before SUBMIT_INTENT and nothing after it.
RESUMABLE = frozenset({PREPARED, NAVIGATING, FILLING, REVIEW_PENDING})

# What an interrupted attempt becomes when it is found in each state. An
# attempt stuck at SUBMIT_INTENT is the dangerous one: we cannot tell from here
# whether the employer received it, so it becomes a question for a person.
ORPHAN_RESOLUTION = {
    SUBMIT_INTENT: VERIFY_REQUIRED,
    SUBMITTED: VERIFY_REQUIRED,
}


class IllegalAttemptTransition(ValueError):
    pass


def can(frm: str, to: str) -> bool:
    return to in ALLOWED.get(frm, frozenset())


def check(frm: str, to: str) -> None:
    if frm not in ALLOWED:
        raise IllegalAttemptTransition(f"unknown attempt state {frm!r}")
    if to not in ALLOWED:
        raise IllegalAttemptTransition(f"unknown target attempt state {to!r}")
    if not can(frm, to):
        allowed = ", ".join(sorted(ALLOWED[frm])) or "nothing (terminal)"
        raise IllegalAttemptTransition(
            f"{frm} -> {to} is not allowed; from {frm} an attempt may only go "
            f"to {allowed}")


def is_resumable(state: str) -> bool:
    return state in RESUMABLE


def resolve_orphan(state: str) -> str | None:
    """What to do with an attempt found RUNNING after a crash. None means it is
    already in a state that needs no resolution."""
    return ORPHAN_RESOLUTION.get(state)
