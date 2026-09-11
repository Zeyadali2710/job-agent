import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from job_agent.core.config import get_settings
from job_agent.core.db import engine

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pool per process. This used to build a second engine of its own
    # (5+5) alongside core.db's (4+4), so the api container could hold 18
    # connections against a 35-connection budget for the whole system.
    # pool_pre_ping, pool_recycle and the UTC server_settings all live in
    # core.db.engine() -- see Phase 3 §0.6.
    engine()
    yield
    await engine().dispose()


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
            async with engine().connect() as conn:
                checks["role"] = (await conn.execute(text("SELECT current_user"))).scalar()
                checks["schema_version"] = (
                    await conn.execute(text("SELECT version_num FROM app.alembic_version"))
                ).scalar()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        return JSONResponse(checks, status_code=503)
    return checks
