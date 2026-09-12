"""The renderer loop. Same shape as workers/loop.py, different privileges.

It claims only `render`, because it is the only process that may read
sensitive.profile_sensitive and the only one with no route to the internet. The
general worker claims everything else; neither can do the other's job, and the
kind filter in core.queue is what keeps that true at runtime rather than only in
the compose file.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.core.queue import claim, complete, fail, reap_stale
from job_agent.generation.render import render_pending

WORKER = os.environ.get("WORKER_NAME", "renderer-1")
_stop = asyncio.Event()

HANDLERS = {
    "render": lambda p: render_pending(p.get("limit", 10),
                                       p.get("application_id")),
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
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop.set)

    tick = 0
    while not _stop.is_set():
        tick += 1
        if tick % 30 == 0:
            async with tx() as conn:
                n = await reap_stale(conn)
                if n:
                    print(f"reaped {n} stale RUNNING items")
        did = await run_once()
        await beat({"idle": did == 0})
        if not did:
            try:
                await asyncio.wait_for(_stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
    print("renderer stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
