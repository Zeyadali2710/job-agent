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
