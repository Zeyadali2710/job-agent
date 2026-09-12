"""The application engine. Runs in the applier, never in the worker.

This is the module DRY_RUN exists for. Three independent layers guard a live
submission and all three are load-bearing:

1. the DRY_RUN environment variable, read once at startup by core.config;
2. `assert_submission_allowed()` below, called inside the submit path rather
   than at its caller, so a new caller inherits the guard instead of having to
   remember it;
3. a database trigger on application_attempts that refuses to record a
   non-dry-run SUBMIT_INTENT while app.settings says dry_run.

Layer 3 is what makes layer 2 more than a comment: the SUBMIT_INTENT row is
committed *before* the click (architecture 10), so a refused insert prevents
the click rather than merely failing to record it.

Under DRY_RUN the whole path still runs. The attempt is written with
dry_run=true, the adapter rehearses, and the application goes back to APPROVED
rather than to SUBMITTED -- because it was not submitted, and a status that
said otherwise would be a lie in the one table you would check first.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.application import attempts as A
from job_agent.application.adapters.base import (
    Document, SubmitContext, SubmitResult,
)
from job_agent.application.adapters.mock import MockAdapter
from job_agent.application.route import transition
from job_agent.core.config import get_settings
from job_agent.core.db import tx


class DryRunViolation(RuntimeError):
    """Raised when something tries to submit for real while DRY_RUN is set."""


def assert_submission_allowed(dry_run: bool, application_id: int) -> None:
    """Layer 2. Inside the submit path, not at the caller.

    Deliberately not an `assert` statement: python -O strips those, and a
    safety property that disappears under an optimisation flag is not one.
    """
    if dry_run:
        raise DryRunViolation(
            f"DRY_RUN is active: refusing to submit application {application_id}")


APPROVED_APPLICATIONS = text("""
SELECT a.id AS app_id, a.status, a.mode, a.job_group_id,
       g.title, c.name AS company, p.canonical_url
  FROM app.applications a
  JOIN app.job_groups g ON g.id = a.job_group_id
  LEFT JOIN app.postings p ON p.id = g.canonical_posting_id
  LEFT JOIN app.companies c ON c.id = g.company_id
 WHERE a.status = 'APPROVED'
 ORDER BY a.id
 LIMIT :n
""")

DOCUMENTS = text("""
SELECT kind, path, sha256 FROM app.generated_documents
 WHERE application_id = :a AND path IS NOT NULL
""")

ANSWERS = text("""
SELECT question_key, answer_text FROM app.application_answers
 WHERE application_id = :a AND can_answer AND answer_text IS NOT NULL
""")

NEXT_ATTEMPT_NO = text("""
SELECT coalesce(max(attempt_no), 0) + 1 FROM app.application_attempts
 WHERE application_id = :a
""")

INSERT_ATTEMPT = text("""
INSERT INTO app.application_attempts (application_id, attempt_no, state, dry_run, adapter)
VALUES (:a, :n, 'PREPARED', :dry, :adapter)
RETURNING id
""")

SET_ATTEMPT = text("""
UPDATE app.application_attempts
   SET state = :state,
       form_hash = coalesce(CAST(:form_hash AS text), form_hash),
       submit_intent_at = CASE WHEN :state = 'SUBMIT_INTENT' THEN now()
                               ELSE submit_intent_at END,
       submitted_at = CASE WHEN :state = 'SUBMITTED' THEN now() ELSE submitted_at END,
       confirmation_ref = coalesce(CAST(:ref AS text), confirmation_ref),
       error = CAST(:error AS text)
 WHERE id = :i
""")

ATTEMPT_STATE = text("SELECT state FROM app.application_attempts WHERE id = :i")

INSERT_ARTIFACT = text("""
INSERT INTO app.attempt_artifacts (attempt_id, kind, path, sha256, bytes)
VALUES (:t, :kind, :path, :sha, :bytes)
""")

EVENT = text("""
INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('application', :a, 'applier', :action, CAST(:d AS jsonb))
""")

# Architecture 10: an attempt found past SUBMIT_INTENT is never auto-retried.
ORPHANS = text("""
SELECT t.id, t.application_id, t.state, t.attempt_no
  FROM app.application_attempts t
 WHERE t.state IN ('SUBMIT_INTENT','SUBMITTED')
   AND t.updated_at < now() - (interval '1 minute' * :m)
   AND NOT EXISTS (SELECT 1 FROM app.application_attempts n
                    WHERE n.application_id = t.application_id
                      AND n.attempt_no > t.attempt_no)
""")

VERIFY_NOTIFICATION = text("""
INSERT INTO app.notifications (kind, dedupe_key, payload)
VALUES ('MANUAL_INTERVENTION_REQUIRED', :key, CAST(:p AS jsonb))
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
""")


async def _set_state(conn, attempt_id: int, to: str, *, form_hash=None,
                     ref=None, error=None) -> None:
    frm = (await conn.execute(ATTEMPT_STATE, {"i": attempt_id})).scalar()
    A.check(frm, to)
    await conn.execute(SET_ATTEMPT, {
        "i": attempt_id, "state": to, "form_hash": form_hash, "ref": ref,
        "error": error})


async def _record_artifacts(conn, attempt_id: int, artifacts) -> None:
    for art in artifacts or ():
        await conn.execute(INSERT_ARTIFACT, {
            "t": attempt_id, "kind": art.kind, "path": art.path,
            "sha": art.sha256, "bytes": art.bytes})


async def resolve_orphans(older_than_minutes: int = 15) -> dict:
    """An attempt interrupted at or past SUBMIT_INTENT becomes VERIFY_REQUIRED.

    Architecture 10 calls this the dangerous case: the click may already have
    landed, so retrying is how one application becomes two in a recruiter's
    inbox. It is a question for a person, not a conclusion the system may draw.
    """
    counts = {"resolved": 0}
    async with tx() as conn:
        rows = (await conn.execute(
            ORPHANS, {"m": older_than_minutes})).mappings().all()
    for row in rows:
        target = A.resolve_orphan(row["state"])
        if target is None:
            continue
        async with tx() as conn:
            await _set_state(conn, row["id"], target,
                             error="interrupted past the point of no return; "
                                   "never auto-retried")
            app_status = (await conn.execute(text(
                "SELECT status FROM app.applications WHERE id = :a"),
                {"a": row["application_id"]})).scalar()
            if app_status == "SUBMITTING":
                await transition(conn, row["application_id"], "SUBMITTING",
                                 "VERIFY_REQUIRED", actor="applier",
                                 reason="attempt interrupted after submit intent")
            await conn.execute(VERIFY_NOTIFICATION, {
                "key": f"verify:{row['application_id']}:{row['attempt_no']}",
                "p": json.dumps({
                    "application_id": row["application_id"],
                    "attempt_no": row["attempt_no"],
                    "reason": "an attempt was interrupted at or after submit "
                              "intent; it was not retried. Check the employer's "
                              "site or your inbox before deciding."})})
        counts["resolved"] += 1
    return counts


async def submit_pending(limit: int = 5, adapter=None) -> dict:
    """Run the attempt lifecycle for APPROVED applications.

    Returns counts. Under DRY_RUN this rehearses every step and submits
    nothing, which is the point: the path is exercised long before it is armed.
    """
    settings = get_settings()
    dry_run = bool(settings.dry_run)
    adapter = adapter or MockAdapter()
    counts = {"attempted": 0, "submitted": 0, "rehearsed": 0, "failed": 0}

    async with tx() as conn:
        rows = (await conn.execute(APPROVED_APPLICATIONS, {"n": limit})).mappings().all()

    for row in rows:
        app_id = row["app_id"]
        async with tx() as conn:
            docs = [Document(d["kind"], d["path"], d["sha256"] or "")
                    for d in (await conn.execute(
                        DOCUMENTS, {"a": app_id})).mappings().all()]
            answers = {a["question_key"]: a["answer_text"] for a in
                       (await conn.execute(ANSWERS, {"a": app_id})).mappings().all()}
            attempt_no = int((await conn.execute(
                NEXT_ATTEMPT_NO, {"a": app_id})).scalar() or 1)
            await transition(conn, app_id, "APPROVED", "SUBMITTING",
                             actor="applier", reason=f"attempt {attempt_no}")
            attempt_id = (await conn.execute(INSERT_ATTEMPT, {
                "a": app_id, "n": attempt_no, "dry": dry_run,
                "adapter": adapter.name})).scalar()
        counts["attempted"] += 1

        ctx = SubmitContext(
            application_id=app_id, job_title=row["title"] or "",
            company=row["company"] or "", job_url=row["canonical_url"] or "",
            documents=docs, answers=answers, dry_run=dry_run)

        try:
            async with tx() as conn:
                await _set_state(conn, attempt_id, A.NAVIGATING)
                await _set_state(conn, attempt_id, A.FILLING)
            prepared = adapter.prepare(ctx)
            async with tx() as conn:
                await _record_artifacts(conn, attempt_id, prepared.artifacts)
                await _set_state(conn, attempt_id, A.REVIEW_PENDING,
                                 form_hash=prepared.form_hash)
        except Exception as exc:
            async with tx() as conn:
                await _set_state(conn, attempt_id, A.FAILED,
                                 error=f"{type(exc).__name__}: {exc}")
                await transition(conn, app_id, "SUBMITTING", "APPROVED",
                                 actor="applier", reason="preparation failed")
            counts["failed"] += 1
            continue

        # Architecture 10: the SUBMIT_INTENT row is committed BEFORE the click,
        # in its own transaction, carrying the form hash. If the process dies
        # in the next line, the row survives and resolve_orphans() turns it
        # into a question rather than a retry.
        async with tx() as conn:
            await _set_state(conn, attempt_id, A.SUBMIT_INTENT,
                             form_hash=prepared.form_hash)

        if dry_run:
            result = SubmitResult(
                submitted=False,
                error="DRY_RUN: rehearsed the submission, sent nothing")
        else:
            assert_submission_allowed(dry_run, app_id)   # layer 2
            result = adapter.submit(ctx, prepared)

        async with tx() as conn:
            await _record_artifacts(conn, attempt_id, result.artifacts)
            if result.submitted:
                await _set_state(conn, attempt_id, A.SUBMITTED,
                                 ref=result.confirmation_ref)
                await transition(conn, app_id, "SUBMITTING", "SUBMITTED",
                                 actor="applier",
                                 reason=f"confirmation {result.confirmation_ref}")
                counts["submitted"] += 1
            else:
                # Not submitted is not failed when it is a rehearsal. The
                # application returns to APPROVED because that is what it still
                # is: approved, and not yet sent.
                await _set_state(conn, attempt_id, A.ABANDONED
                                 if dry_run else A.FAILED, error=result.error)
                await transition(conn, app_id, "SUBMITTING", "APPROVED",
                                 actor="applier",
                                 reason=result.error or "not submitted")
                counts["rehearsed" if dry_run else "failed"] += 1
            await conn.execute(EVENT, {
                "a": app_id,
                "action": "attempt_rehearsed" if dry_run else "attempt_finished",
                "d": json.dumps({"attempt_no": attempt_no, "adapter": adapter.name,
                                 "dry_run": dry_run, "submitted": result.submitted,
                                 "form_hash": prepared.form_hash,
                                 "error": result.error})})
    return counts
