"""Shared async engine. pool_pre_ping is not optional — see Phase 3 §0.6."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from job_agent.core.config import get_settings

_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            pool_size=4, max_overflow=4,
            pool_pre_ping=True, pool_recycle=1800, pool_timeout=10,
            connect_args={
                "timeout": 5,
                "server_settings": {
                    "application_name": os.environ.get("WORKER_NAME", "job-agent"),
                    "timezone": "UTC",
                },
            },
        )
    return _engine


@asynccontextmanager
async def tx():
    async with engine().begin() as conn:
        yield conn
