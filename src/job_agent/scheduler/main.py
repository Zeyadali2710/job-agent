from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from job_agent.core.config import get_settings
from job_agent.core.db import tx
from job_agent.core.queue import enqueue

SYDNEY = ZoneInfo("Australia/Sydney")
logging.basicConfig(level=logging.INFO)


async def enqueue_poll(source: str) -> None:
    """Scheduler ONLY enqueues. It never fetches."""
    async with tx() as conn:
        wid = await enqueue(conn, "poll_source", {"source": source},
                            dedupe_key=f"poll:{source}", priority=50)
    logging.info("enqueued poll %s -> %s", source, wid or "deduped")

async def _enqueue_kind(kind: str) -> None:
    async with tx() as conn:
        await enqueue(conn, kind, {}, dedupe_key=kind, priority=80)


async def enqueue_dedupe() -> None:
    await _enqueue_kind("dedupe")


async def enqueue_screen() -> None:
    await _enqueue_kind("screen")


async def enqueue_score() -> None:
    await _enqueue_kind("score")


async def enqueue_enrich() -> None:
    await _enqueue_kind("enrich")


async def enqueue_notify_matches() -> None:
    await _enqueue_kind("notify_matches")


async def enqueue_draft() -> None:
    await _enqueue_kind("draft")


async def enqueue_generate() -> None:
    await _enqueue_kind("generate")


async def enqueue_route() -> None:
    await _enqueue_kind("route")


async def enqueue_resolve_orphans() -> None:
    await _enqueue_kind("resolve_orphans")


async def daily_summary() -> None:
    async with tx() as conn:
        await enqueue(conn, "daily_summary", {}, dedupe_key=None, priority=200)


def build() -> AsyncIOScheduler:
    s = get_settings()
    sched = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(
            url=s.sync_database_url, tablename="apscheduler_jobs",
            tableschema="app")},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
        timezone=SYDNEY,
    )
    sched.add_job(enqueue_poll, CronTrigger(minute="*/30", timezone=SYDNEY),
                  args=["greenhouse"], id="poll:greenhouse", jitter=90, replace_existing=True)
    sched.add_job(enqueue_poll, CronTrigger(minute="7,37", timezone=SYDNEY),
                  args=["lever"], id="poll:lever", jitter=90, replace_existing=True)
    sched.add_job(enqueue_poll, CronTrigger(minute="*/30", timezone=SYDNEY),
                  args=["ashby"], id="poll:ashby", jitter=90, replace_existing=True)
    # config/companies.yaml carries 4 workable tenants; without this job they
    # were only ever polled by running discovery.py by hand.
    sched.add_job(enqueue_poll, CronTrigger(minute="17,47", timezone=SYDNEY),
                  args=["workable"], id="poll:workable", jitter=90, replace_existing=True)
    sched.add_job(daily_summary, CronTrigger(hour=19, minute=0, timezone=SYDNEY),
                  id="daily_summary", replace_existing=True)
    sched.add_job(enqueue_dedupe, CronTrigger(minute="*/10", timezone=SYDNEY),
                  id="dedupe", replace_existing=True)
    sched.add_job(enqueue_screen, CronTrigger(minute="*/10", timezone=SYDNEY),
                  id="screen", replace_existing=True)
    # The pipeline runs in spend order: screen (free) at :00, enrich (paid,
    # survivors only) at :02, score (free, reads the enrichment) at :05,
    # notify at :07. Each stage reads what the one before it wrote.
    sched.add_job(enqueue_enrich, CronTrigger(minute="2,12,22,32,42,52", timezone=SYDNEY),
                  id="enrich", replace_existing=True)
    sched.add_job(enqueue_score, CronTrigger(minute="5,15,25,35,45,55", timezone=SYDNEY),
                  id="score", replace_existing=True)
    sched.add_job(enqueue_notify_matches,
                  CronTrigger(minute="7,17,27,37,47,57", timezone=SYDNEY),
                  id="notify_matches", replace_existing=True)
    # Once a day, not hourly. Drafting is the most expensive thing this system
    # does and the set of jobs worth drafting for does not change between 09:25
    # and 10:25. The arithmetic that settled it, at measured prices:
    #
    #   cover letter  0.026 AUD   answers  0.028 AUD   = 0.054 per application
    #   hourly x 5 drafts = 6.5 AUD/day  -> the 7 AUD monthly cap in one day
    #   daily  x 5 drafts = 8.1 AUD/month -> still over the cap
    #   daily  x 2 drafts = 3.2 AUD/month -> fits, with room for enrichment
    #
    # So the cadence and generate.max_drafts_per_run are one decision, not two.
    # 07:25 Sydney puts the drafts in front of you before the working day
    # rather than accumulating overnight.
    sched.add_job(enqueue_draft, CronTrigger(hour=7, minute=25, timezone=SYDNEY),
                  id="draft", replace_existing=True)
    sched.add_job(enqueue_generate, CronTrigger(hour=7, minute=30, timezone=SYDNEY),
                  id="generate", replace_existing=True)
    # Routing is free -- no model, pure rules -- so it runs often enough to pick
    # up an application as soon as its documents finish rendering.
    sched.add_job(enqueue_route, CronTrigger(minute="9,39", timezone=SYDNEY),
                  id="route", replace_existing=True)
    # `submit` is deliberately not on a cron: approval enqueues it. A DRY_RUN
    # rehearsal returns the application to APPROVED, so a scheduled submit
    # would rehearse the same application on every tick forever.
    # Orphan resolution is: an attempt interrupted past SUBMIT_INTENT must
    # become a question quickly, not at the next approval.
    sched.add_job(enqueue_resolve_orphans,
                  CronTrigger(minute="14,44", timezone=SYDNEY),
                  id="resolve_orphans", replace_existing=True)
    return sched


async def main() -> None:
    sched = build()
    sched.start()
    async with tx() as conn:
        await conn.execute(text(
            "INSERT INTO app.heartbeats (worker, last_beat) VALUES ('scheduler', now()) "
            "ON CONFLICT (worker) DO UPDATE SET last_beat = now()"))
    logging.info("scheduler running: %s", [j.id for j in sched.get_jobs()])
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
