"""Notifier worker: drain the outbox to Discord. Architecture 7.

Runs as ja_notifier, which has no reach into schema sensitive and no profile
read. It claims with FOR UPDATE SKIP LOCKED inside one transaction per batch,
so a restart mid-batch cannot double-post.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal

import httpx
from sqlalchemy import text

from job_agent.core.config import get_settings
from job_agent.core.db import tx
from job_agent.notifications import cards, discord
from job_agent.notifications.outbox import claim, mark_failed, mark_sent, redact

WORKER = os.environ.get("WORKER_NAME", "notifier")
# Small on purpose: the claim transaction stays open across the whole
# batch. See the TIMEOUT/MAX_SLEEP note in notifications/discord.py.
BATCH = 3
IDLE_SECONDS = 10
_stop = asyncio.Event()


async def beat(conn, detail: dict) -> None:
    await conn.execute(text(
        "INSERT INTO app.heartbeats (worker, last_beat, detail) "
        "VALUES (:w, now(), CAST(:d AS jsonb)) "
        "ON CONFLICT (worker) DO UPDATE SET last_beat=now(), detail=EXCLUDED.detail"),
        {"w": WORKER, "d": json.dumps(detail)})


async def _event(conn, nid: int, action: str, detail: dict) -> None:
    await conn.execute(text(
        "INSERT INTO app.events (entity_type, entity_id, actor, action, detail) "
        "VALUES ('notification', :i, :w, :a, CAST(:d AS jsonb))"),
        {"i": nid, "w": WORKER, "a": action, "d": json.dumps(detail)})


async def drain_once(webhook: str, hostname: str, client: httpx.AsyncClient) -> int:
    """One transaction: claim a batch, send each, record each. Returns the
    number of rows handled."""
    sent = failed = 0
    async with tx() as conn:
        rows = await claim(conn, BATCH)
        for row in rows:
            attempts = row["attempts"] + 1
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload or "{}")
            try:
                body = cards.render(row["kind"], payload, hostname)
                await discord.send(webhook, body, client=client)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                state = await mark_failed(conn, row["id"], attempts, err)
                await _event(conn, row["id"], "send_failed",
                             {"state": state, "attempts": attempts,
                              "error": redact(err)[:300]})
                failed += 1
            else:
                await mark_sent(conn, row["id"], attempts)
                await _event(conn, row["id"], "sent", {"kind": row["kind"]})
                sent += 1
        if rows:
            await beat(conn, {"sent": sent, "failed": failed})
        else:
            await beat(conn, {"idle": True})
    return len(rows)


async def main() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, _stop.set)

    s = get_settings()
    # Read once, at startup, so a missing secret fails immediately and loudly
    # rather than on the first notification hours later.
    webhook = s.discord_webhook
    hostname = s.tailnet_hostname
    print(f"notifier started: webhook configured, dashboard={hostname or 'unset'}")

    async with httpx.AsyncClient(timeout=discord.TIMEOUT) as client:
        while not _stop.is_set():
            try:
                handled = await drain_once(webhook, hostname, client)
            except Exception as exc:
                # A database error must not kill the worker; the rows stay
                # PENDING and the next pass retries them.
                print(f"drain failed: {type(exc).__name__}: {redact(str(exc))}")
                handled = 0
            if not handled:
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=IDLE_SECONDS)
                except asyncio.TimeoutError:
                    pass
    print("notifier stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
