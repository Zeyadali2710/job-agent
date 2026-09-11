import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from job_agent.core.config import get_settings

settings = get_settings()
engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,      # validate a pooled connection before handing it out
        pool_recycle=1800,       # never reuse a connection older than 30 minutes
        pool_timeout=10,
        connect_args={
            "timeout": 5,
            "server_settings": {
                "application_name": "job-agent-api",
                "timezone": "UTC",
            },
        },
    )
    yield
    await engine.dispose()


app = FastAPI(title="Job Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    checks = {
        "api": "ok",
        "database": "unknown",
        "dry_run": settings.dry_run,
        "role": None,
        "schema_version": None,
    }
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as conn:
                checks["role"] = (await conn.execute(text("SELECT current_user"))).scalar()
                checks["schema_version"] = (
                    await conn.execute(text("SELECT version_num FROM app.alembic_version"))
                ).scalar()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        return JSONResponse(checks, status_code=503)
    return checks