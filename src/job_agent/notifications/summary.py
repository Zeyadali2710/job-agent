"""Daily summary: count the day, write one outbox row, stop there.

The scheduler enqueues kind='daily_summary' at 19:00 Sydney. This handler does
not talk to Discord -- it inserts a DAILY_SUMMARY row into app.notifications
and lets Phase 10's notifier drain the outbox. That is the transactional
outbox the architecture specifies: the counting transaction and the delivery
attempt must not be the same transaction.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from sqlalchemy import text

from job_agent.core.db import tx

STATS_SQL = text("""
SELECT
  (SELECT count(*) FROM app.job_groups
    WHERE first_seen_at > now() - interval '24 hours')                      AS new_groups,
  (SELECT count(*) FROM app.job_groups WHERE status='READY')                AS ready,
  (SELECT count(*) FROM app.job_groups WHERE status='REVIEW_REQUIRED')      AS review,
  (SELECT count(*) FROM app.job_groups
    WHERE status='SCREENED_OUT' AND updated_at > now() - interval '24 hours') AS screened_out_24h,
  (SELECT count(*) FROM app.postings
    WHERE first_seen_at > now() - interval '24 hours')                      AS new_postings,
  (SELECT count(*) FROM app.work_items WHERE state IN ('PENDING','RUNNING')) AS queued,
  (SELECT count(*) FROM app.work_items WHERE state='DEAD')                  AS dead,
  (SELECT count(*) FROM app.sources WHERE circuit_state='OPEN')             AS circuits_open
""")

REASONS_SQL = text("""
SELECT screen_reason AS reason, count(*) AS count
  FROM app.job_groups
 WHERE status='SCREENED_OUT' AND screen_reason IS NOT NULL
   AND updated_at > now() - interval '24 hours'
 GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5
""")

INSERT_NOTIFICATION = text("""
INSERT INTO app.notifications (kind, dedupe_key, payload)
VALUES ('DAILY_SUMMARY', :key, CAST(:payload AS jsonb))
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
RETURNING id
""")

COUNT_FIELDS = ("new_groups", "ready", "review", "screened_out_24h",
                "new_postings", "queued", "dead", "circuits_open")


def dedupe_key(now: datetime | None = None) -> str:
    """One summary per day.

    The UTC date, not the Sydney date. The job fires at 19:00 Sydney, which is
    08:00 or 09:00 UTC the SAME calendar day in both AEST and AEDT, and
    misfire_grace_time is 900s so it cannot drift past 19:15 -- so the UTC date
    already labels the right day. Converting here would put a second
    UTC-to-Sydney boundary in the codebase, and syd() in web/routes.py is meant
    to be the only one.
    """
    now = now or datetime.now(timezone.utc)
    return f"daily_summary:{now.astimezone(timezone.utc).date().isoformat()}"


def summarise(stats: Mapping, reasons: Sequence[Mapping],
              now: datetime | None = None) -> dict:
    """Pure: rows in, notification payload out. No I/O."""
    payload = {"date": dedupe_key(now).split(":", 1)[1]}
    payload.update({f: int(stats[f]) for f in COUNT_FIELDS})
    payload["top_screen_reasons"] = [
        {"reason": r["reason"], "count": int(r["count"])} for r in reasons
    ]
    return payload


async def daily_summary() -> dict:
    async with tx() as conn:
        stats = (await conn.execute(STATS_SQL)).mappings().first()
        reasons = (await conn.execute(REASONS_SQL)).mappings().all()
        payload = summarise(stats, reasons)
        key = dedupe_key()
        nid = (await conn.execute(
            INSERT_NOTIFICATION, {"key": key, "payload": json.dumps(payload)})).scalar()
        await conn.execute(text(
            "INSERT INTO app.events (entity_type, entity_id, actor, action, detail) "
            "VALUES ('notification', :i, 'summary', :a, CAST(:d AS jsonb))"),
            {"i": nid, "a": "queued" if nid else "already_queued",
             "d": json.dumps({"dedupe_key": key})})
    return payload
