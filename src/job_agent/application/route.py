"""Route drafted applications: are they ready, and where do they go.

Nothing here submits anything. The furthest this can move an application is
APPROVED, which is a statement that it may be sent, not an act of sending.
All three DRY_RUN layers are downstream of here and untouched: this module
never inserts an application_attempt, which is the row the database trigger
guards.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.ai.client import decode_jsonb
from job_agent.application import rules as R
from job_agent.application.transitions import check
from job_agent.core.db import tx
from job_agent.core.queue import enqueue


def advisories(docs) -> list[str]:
    """MERGED_SOURCES findings across an application's documents. Advisory
    only: validator_state is already PASSED or the application would not be
    here, so these are things to read, not reasons to stop."""
    out = []
    for d in docs:
        for f in decode_jsonb(d.get("validator_findings")) or ():
            if isinstance(f, dict) and f.get("kind") == "MERGED_SOURCES":
                out.append(str(f.get("detail") or ""))
    return out

CANDIDATES = text("""
SELECT a.id AS app_id, a.status, a.mode, a.job_group_id,
       g.title, g.is_grad_program, g.location_class,
       c.slug AS company_slug, c.name AS company,
       s.score
  FROM app.applications a
  JOIN app.job_groups g ON g.id = a.job_group_id
  LEFT JOIN app.companies c ON c.id = g.company_id
  LEFT JOIN app.current_job_scores s ON s.job_group_id = g.id
 WHERE a.status = 'DRAFT'
 ORDER BY s.score DESC NULLS LAST, a.id
 LIMIT :n
""")

DOCUMENTS = text("""
SELECT kind, validator_state, path, validator_findings
  FROM app.generated_documents
 WHERE application_id = :a
""")

OPEN_REQUIRED = text("""
SELECT count(*) FROM app.application_answers
 WHERE application_id = :a AND required AND NOT can_answer
""")

RULES = text("""
SELECT name, enabled, priority, match, action, company_allowlist
  FROM app.application_rules WHERE enabled
""")

# Approvals granted today, whether by a rule or by a person: the cap is on
# applications the system is willing to send, not on how they were blessed.
APPROVED_TODAY = text("""
SELECT count(*) FROM app.application_status_history
 WHERE to_status = 'APPROVED' AND at >= date_trunc('day', now())
""")

SETTING = text("SELECT value FROM app.settings WHERE key = :k")

# COALESCE so a transition that has no opinion about mode leaves it alone;
# mode is NOT NULL, so passing None straight through would fail the write.
# The cast is required for the same reason the queue's kind filter needs one.
SET_STATUS = text("""
UPDATE app.applications
   SET status = :to, mode = COALESCE(CAST(:mode AS smallint), mode)
 WHERE id = :a
""")

HISTORY = text("""
INSERT INTO app.application_status_history
  (application_id, from_status, to_status, actor, reason)
VALUES (:a, :frm, :to, :actor, :reason)
""")

EVENT = text("""
INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('application', :a, :actor, :action, CAST(:d AS jsonb))
""")

APPROVAL_NOTIFICATION = text("""
INSERT INTO app.notifications (kind, dedupe_key, payload)
VALUES ('APPROVAL_REQUIRED', :key, CAST(:p AS jsonb))
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
""")


async def transition(conn, app_id: int, from_status: str, to_status: str, *,
                     actor: str, reason: str, mode: int | None = None) -> None:
    """The only way an application changes status. Checks the transition table
    first, so an illegal move raises before any row is written."""
    check(from_status, to_status)
    await conn.execute(SET_STATUS, {"a": app_id, "to": to_status, "mode": mode})
    await conn.execute(HISTORY, {"a": app_id, "frm": from_status, "to": to_status,
                                 "actor": actor, "reason": reason[:500]})
    await conn.execute(EVENT, {
        "a": app_id, "actor": actor, "action": f"status_{to_status.lower()}",
        "d": json.dumps({"from": from_status, "to": to_status, "reason": reason})})


async def route_pending(limit: int = 20) -> dict:
    counts = {"routed": 0, "held": 0, "approved": 0, "awaiting": 0,
              "withdrawn": 0, "blocked": {}}

    async with tx() as conn:
        rows = (await conn.execute(CANDIDATES, {"n": limit})).mappings().all()
        rule_rows = [dict(r) for r in (await conn.execute(RULES)).mappings().all()]
        cap_raw = (await conn.execute(
            SETTING, {"k": "daily_application_cap"})).scalar()
        approved_today = int((await conn.execute(APPROVED_TODAY)).scalar() or 0)

    cap = int(json.loads(cap_raw) if isinstance(cap_raw, str) else (cap_raw or 8))

    for row in rows:
        async with tx() as conn:
            docs = [dict(d) for d in (await conn.execute(
                DOCUMENTS, {"a": row["app_id"]})).mappings().all()]
            open_required = int((await conn.execute(
                OPEN_REQUIRED, {"a": row["app_id"]})).scalar() or 0)

            blockers = R.readiness(docs, open_required)
            if blockers:
                counts["held"] += 1
                for b in blockers:
                    counts["blocked"][b] = counts["blocked"].get(b, 0) + 1
                continue

            # DRAFT -> READY first: "the documents are good" is a separate
            # statement from "this is where it should go", and the history
            # should show both.
            await transition(conn, row["app_id"], "DRAFT", "READY",
                             actor="route", reason="documents generated and validated")

            job = {"score": float(row["score"] or 0), "title": row["title"],
                   "company_slug": row["company_slug"],
                   "location_class": row["location_class"],
                   "is_grad_program": row["is_grad_program"]}
            decision = R.decide(rule_rows, job,
                                auto_apply_allowed=approved_today < cap)

            await transition(conn, row["app_id"], "READY", decision.status,
                             actor="route", reason=decision.reason,
                             mode=decision.mode)

            if decision.status == "AWAITING_APPROVAL":
                await conn.execute(APPROVAL_NOTIFICATION, {
                    "key": f"approval:{row['app_id']}",
                    "p": json.dumps({
                        "application_id": row["app_id"],
                        "job_group_id": row["job_group_id"],
                        "title": row["title"], "company": row["company"],
                        "score": float(row["score"] or 0),
                        "rule": decision.rule, "reason": decision.reason,
                        "documents": sorted(d["kind"] for d in docs),
                        # Advisories go on the card, not just in the dashboard:
                        # a paragraph that merges two engagements is exactly
                        # what a 90-second phone review needs to see first.
                        "advisories": advisories(docs)})})
                counts["awaiting"] += 1
            elif decision.status == "APPROVED":
                await enqueue(conn, "submit",
                              {"application_id": row["app_id"], "limit": 5},
                              dedupe_key=f"submit:{row['app_id']}")
                approved_today += 1
                counts["approved"] += 1
            else:
                counts["withdrawn"] += 1
            counts["routed"] += 1

    return counts


async def set_status(app_id: int, to_status: str, *, actor: str,
                     reason: str = "") -> dict:
    """Used by the dashboard. Reads the current status inside the transaction
    so two people clicking approve cannot both succeed against a stale read."""
    async with tx() as conn:
        row = (await conn.execute(text(
            "SELECT status FROM app.applications WHERE id = :a FOR UPDATE"),
            {"a": app_id})).first()
        if row is None:
            raise ValueError(f"no application {app_id}")
        await transition(conn, app_id, row.status, to_status,
                         actor=actor, reason=reason or f"{actor} via dashboard")
        if to_status == "APPROVED":
            # Not a cron. An approved application would otherwise be rehearsed
            # on every tick forever, because a DRY_RUN rehearsal returns it to
            # APPROVED. The dedupe key only holds while the item is PENDING or
            # RUNNING, so a later deliberate re-attempt still enqueues.
            await enqueue(conn, "submit", {"application_id": app_id, "limit": 5},
                          dedupe_key=f"submit:{app_id}")
    return {"application_id": app_id, "from": row.status, "to": to_status}
