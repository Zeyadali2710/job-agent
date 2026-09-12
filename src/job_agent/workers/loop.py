from __future__ import annotations

import asyncio
import json
import os
import signal

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.core.queue import claim, complete, fail, reap_stale
from job_agent.notifications.summary import daily_summary
from job_agent.workers.discovery import poll_source
from job_agent.pipeline.run import dedupe_pending
from job_agent.pipeline.screen import screen_pending
from job_agent.pipeline.score import score_pending
from job_agent.pipeline.enrich import enrich_pending
from job_agent.notifications.matches import notify_high_matches
from job_agent.generation.draft import draft_applications
from job_agent.generation.documents import generate_pending
from job_agent.generation.answers import answer_questions
from job_agent.application.route import route_pending

WORKER = os.environ.get("WORKER_NAME", "worker-1")
_stop = asyncio.Event()

HANDLERS = {
    "poll_source": lambda p: poll_source(p["source"]),
    "dedupe": lambda p: dedupe_pending(p.get("limit", 200)),
    "screen": lambda p: screen_pending(p.get("limit", 200)),
    "enrich": lambda p: enrich_pending(p.get("limit", 25)),
    "score": lambda p: score_pending(p.get("limit", 200)),
    "notify_matches": lambda p: notify_high_matches(p.get("limit", 20)),
    "draft": lambda p: draft_applications(p.get("limit")),
    # `render` is deliberately absent: it belongs to the renderer, which is the
    # only role with USAGE on schema sensitive.
    "generate": lambda p: generate_pending(p.get("limit", 5),
                                           p.get("application_id")),
    "answer": lambda p: answer_questions(p["application_id"]),
    "route": lambda p: route_pending(p.get("limit", 20)),
    "daily_summary": lambda p: daily_summary(),
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
        handler = HANDLERS.get(item.kind)
        if handler is None:
            raise ValueError(f"no handler for kind {item.kind!r}")
        await handler(item.payload)
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
    print("worker stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
