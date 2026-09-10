import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from job_agent.core.config import get_settings

settings = get_settings()
engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=5)
    yield
    await engine.dispose()


app = FastAPI(title="Job Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    checks = {"api": "ok", "database": "unknown", "dry_run": settings.dry_run}
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        return JSONResponse(checks, status_code=503)
    return checks


@app.get("/")
async def index():
    return {"service": "job-agent", "phase": 2, "dry_run": settings.dry_run}