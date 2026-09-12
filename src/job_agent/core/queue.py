from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

# :kinds is NULL for "anything I can handle" and a list for a worker that owns
# only some kinds. The renderer must not claim a `screen` item, and -- more to
# the point -- the worker must not claim a `render` item: ja_worker has no USAGE
# on schema sensitive, so it would fail every one of them eight times.
CLAIM = text("""
WITH claimed AS (
  SELECT id FROM app.work_items
   WHERE state = 'PENDING' AND run_after <= now()
     AND (:kinds IS NULL OR kind = ANY(CAST(:kinds AS text[])))
   ORDER BY priority, run_after
   FOR UPDATE SKIP LOCKED
   LIMIT :n
)
UPDATE app.work_items w
   SET state='RUNNING', locked_by=:worker, locked_at=now(), attempts=w.attempts+1
  FROM claimed c
 WHERE w.id = c.id
RETURNING w.id, w.kind, w.payload, w.attempts, w.max_attempts
""")


async def enqueue(conn, kind: str, payload: dict[str, Any] | None = None,
                  dedupe_key: str | None = None, priority: int = 100) -> int | None:
    row = (await conn.execute(text("""
        INSERT INTO app.work_items (kind, payload, dedupe_key, priority)
        VALUES (:k, CAST(:p AS jsonb), :d, :pri)
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
          AND state IN ('PENDING','RUNNING') DO NOTHING
        RETURNING id"""),
        {"k": kind, "p": json.dumps(payload or {}), "d": dedupe_key, "pri": priority})).scalar()
    return row


async def claim(conn, worker: str, n: int = 1, kinds: list[str] | None = None):
    return (await conn.execute(
        CLAIM, {"worker": worker, "n": n,
                "kinds": list(kinds) if kinds else None})).all()


async def complete(conn, item_id: int) -> None:
    await conn.execute(text(
        "UPDATE app.work_items SET state='DONE', locked_by=NULL, locked_at=NULL WHERE id=:i"),
        {"i": item_id})


async def fail(conn, item_id: int, err: str, attempts: int, max_attempts: int) -> None:
    dead = attempts >= max_attempts
    await conn.execute(text("""
        UPDATE app.work_items
           SET state = :s, last_error = :e, locked_by = NULL, locked_at = NULL,
               run_after = now() + (interval '1 minute' * power(3, LEAST(:a, 5)))
         WHERE id = :i"""),
        {"s": "DEAD" if dead else "PENDING", "e": err[:2000], "a": attempts, "i": item_id})


async def reap_stale(conn, older_than_minutes: int = 30) -> int:
    """A worker killed mid-item leaves RUNNING forever. Return it to PENDING."""
    return (await conn.execute(text("""
        UPDATE app.work_items
           SET state='PENDING', locked_by=NULL, locked_at=NULL
         WHERE state='RUNNING' AND locked_at < now() - (interval '1 minute' * :m)
        RETURNING id"""), {"m": older_than_minutes})).rowcount
