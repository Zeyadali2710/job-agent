"""The applier loop. Claims only `submit` and `resolve_orphans`.

Separate from the worker for the same reason the renderer is: different
privileges. ja_applier is the only role with INSERT on application_attempts,
which is the table the DRY_RUN trigger guards, and it holds no API key and no
read of the profile beyond the documents already generated.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal

from sqlalchemy import text

from job_agent.application.engine import resolve_orphans, submit_pending
from job_agent.core.db import tx
from job_agent.core.queue import claim, complete, fail, reap_stale

WORKER = os.environ.get("WORKER_NAME", "applier-1")
_stop = asyncio.Event()

HANDLERS = {
    "submit": lambda p: submit_pending(p.get("limit", 5)),
    "resolve_orphans": lambda p: resolve_orphans(p.get("older_than_minutes", 15)),
}


async def beat(detail: dict) -> None:
    async with tx() as conn:
        await conn.execute(text(
            "INSERT INTO app.heartbeats (worker, last_beat, detail) "
            "VALUES (:w, now(), CAST(:d AS jsonb)) "
            "ON CONFLICT (worker) DO UPDATE SET last_beat=now(), detail=EXCLUDED.detail"),
            {"w": WORKER, "d": json.dumps(detail)})


async def run_once() -> int:
    async with tx() as conn:
        items = await claim(conn, WORKER, n=1, kinds=list(HANDLERS))
    if not items:
        return 0
    item = items[0]
    try:
        await HANDLERS[item.kind](item.payload)
        async with tx() as conn:
            await complete(conn, item.id)
        return 1
    except Exception as exc:
        async with tx() as conn:
            await fail(conn, item.id, f"{type(exc).__name__}: {exc}",
                       item.attempts, item.max_attempts)
        print(f"item {item.id} ({item.kind}) failed: {exc}")
        return 1


async def main() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, _stop.set)

    # Before claiming anything, deal with whatever the last crash left behind.
    # Architecture 10: an attempt past SUBMIT_INTENT is never auto-retried, and
    # the moment to notice that is startup, not the next scheduled tick.
    async with tx() as conn:
        await reap_stale(conn)
    try:
        n = await resolve_orphans()
        if n["resolved"]:
            print(f"resolved {n['resolved']} interrupted attempts to VERIFY_REQUIRED")
    except Exception as exc:            # never block startup on recovery
        print(f"orphan resolution failed at startup: {exc}")

    tick = 0
    while not _stop.is_set():
        tick += 1
        if tick % 30 == 0:
            async with tx() as conn:
                await reap_stale(conn)
        did = await run_once()
        await beat({"idle": did == 0})
        if not did:
            try:
                await asyncio.wait_for(_stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
    print("applier stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
