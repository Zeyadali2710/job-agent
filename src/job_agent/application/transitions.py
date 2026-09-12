"""Legal application status transitions.

`applications.status` has a CHECK constraint listing the valid *values*, but
nothing in the schema says which value may follow which. Without that, a bug
can move a WITHDRAWN application back to APPROVED and the database will hold
it happily. This is the missing half, and it is a pure function so the whole
table is testable without a database.

Phase 14 owns everything past APPROVED; those edges are declared here so the
table is complete and the applier does not get to invent its own.
"""
from __future__ import annotations

ALLOWED: dict[str, frozenset[str]] = {
    # Drafting. READY means the documents exist and passed; routing then
    # decides where it goes.
    "DRAFT": frozenset({"READY", "WITHDRAWN", "FAILED_PERMANENT"}),
    "READY": frozenset({"AWAITING_APPROVAL", "APPROVED", "WITHDRAWN",
                        "FAILED_PERMANENT"}),
    # A person acts here, or changes their mind and sends it back.
    "AWAITING_APPROVAL": frozenset({"APPROVED", "WITHDRAWN", "DRAFT",
                                    "FAILED_PERMANENT"}),
    # Approval is revocable right up until something is actually sent.
    "APPROVED": frozenset({"SUBMITTING", "AWAITING_APPROVAL", "WITHDRAWN",
                           "FAILED_PERMANENT"}),
    "SUBMITTING": frozenset({"SUBMITTED", "VERIFY_REQUIRED", "FAILED_PERMANENT",
                             "APPROVED"}),
    "SUBMITTED": frozenset({"CONFIRMED", "VERIFY_REQUIRED", "FAILED_PERMANENT"}),
    "VERIFY_REQUIRED": frozenset({"CONFIRMED", "SUBMITTED", "FAILED_PERMANENT",
                                  "WITHDRAWN"}),
    # Terminal. Nothing follows a confirmed application; the partial unique
    # index treats WITHDRAWN and FAILED_PERMANENT as free slots, so a
    # deliberate re-application is a new row rather than a resurrected one.
    "CONFIRMED": frozenset(),
    "WITHDRAWN": frozenset(),
    "FAILED_PERMANENT": frozenset(),
}

TERMINAL = frozenset(s for s, nxt in ALLOWED.items() if not nxt)


class IllegalTransition(ValueError):
    pass


def can(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED.get(from_status, frozenset())


def check(from_status: str, to_status: str) -> None:
    """Raise unless the move is legal. Called before the UPDATE, so a rejected
    transition leaves no status_history row claiming it happened."""
    if from_status not in ALLOWED:
        raise IllegalTransition(f"unknown status {from_status!r}")
    if to_status not in ALLOWED:
        raise IllegalTransition(f"unknown target status {to_status!r}")
    if not can(from_status, to_status):
        allowed = ", ".join(sorted(ALLOWED[from_status])) or "nothing (terminal)"
        raise IllegalTransition(
            f"{from_status} -> {to_status} is not allowed; from {from_status} "
            f"an application may only go to {allowed}")
