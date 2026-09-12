"""Transactional outbox drain. Architecture 7.

The pipeline writes notifications in the same transaction as the state change
that caused them. This module is the other half: claim PENDING rows, and record
what happened. It never talks to Discord -- see discord.py -- so the pipeline is
never blocked on network I/O and a Discord outage costs nothing but a backlog.
"""
from __future__ import annotations

import re

from sqlalchemy import text

# Architecture 7: 1m, 5m, 15m, 1h, 6h. notifications.attempts carries
# CHECK (attempts <= 8), so later attempts clamp to the last interval and the
# 8th failure lands exactly on 8 -- never 9, which the CHECK would reject.
BACKOFF_MINUTES = (1, 5, 15, 60, 360)
MAX_ATTEMPTS = 8

# The webhook URL is a bearer credential: anyone holding it can post as you.
# It reaches us mainly through httpx.HTTPStatusError, whose message embeds the
# full request URL, and that string is written to notifications.last_error and
# app.events. Redact at the boundary rather than trusting every call site.
WEBHOOK_RE = re.compile(r"https?://(?:\w+\.)*discord(?:app)?\.com/api/webhooks/\S+", re.I)


def redact(s: str | None) -> str | None:
    if not s:
        return s
    return WEBHOOK_RE.sub("https://discord.com/api/webhooks/<redacted>", s)


def backoff_minutes(attempts: int) -> int:
    """Minutes to wait before attempt number `attempts` + 1."""
    idx = min(max(attempts, 1), len(BACKOFF_MINUTES)) - 1
    return BACKOFF_MINUTES[idx]


def is_dead(attempts: int) -> bool:
    return attempts >= MAX_ATTEMPTS


CLAIM = text("""
SELECT id, kind, payload, attempts
  FROM app.notifications
 WHERE state = 'PENDING' AND next_attempt_at <= now()
 ORDER BY next_attempt_at, id
 FOR UPDATE SKIP LOCKED
 LIMIT :n
""")

MARK_SENT = text("""
UPDATE app.notifications
   SET state='SENT', sent_at=now(), attempts=:a, last_error=NULL
 WHERE id=:i
""")

MARK_FAILED = text("""
UPDATE app.notifications
   SET state=:s, attempts=:a, last_error=:e,
       next_attempt_at = now() + (interval '1 minute' * :m)
 WHERE id=:i
""")


async def claim(conn, n: int = 5):
    """SKIP LOCKED so a second notifier never double-posts the same row."""
    return (await conn.execute(CLAIM, {"n": n})).mappings().all()


async def mark_sent(conn, nid: int, attempts: int) -> None:
    await conn.execute(MARK_SENT, {"i": nid, "a": attempts})


async def mark_failed(conn, nid: int, attempts: int, err: str) -> str:
    """Returns the new state so the caller can log it. Redacts here, not at the
    call site, so no future caller can leak the webhook into last_error."""
    state = "DEAD" if is_dead(attempts) else "PENDING"
    await conn.execute(MARK_FAILED, {
        "i": nid, "s": state, "a": attempts,
        "e": (redact(err) or "")[:2000],
        "m": backoff_minutes(attempts),
    })
    return state
