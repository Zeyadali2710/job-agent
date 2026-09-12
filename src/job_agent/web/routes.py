from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from job_agent.core.config import get_settings
from job_agent.core.db import engine

SYD = ZoneInfo("Australia/Sydney")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
settings = get_settings()


def syd(dt: datetime | None) -> str | None:
    """The ONLY place UTC becomes Sydney. Storage and computation stay UTC.

    Uses the named zone, never a fixed offset: Sydney is +10 in winter and +11
    in summer, and a hardcoded +10:00 is wrong for about half the year.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SYD).strftime("%Y-%m-%d %H:%M")


def as_dict(value: Any) -> dict:
    """jsonb columns come back as dict or as str depending on the driver's
    codec registration. screen.py::_profile already guards this both ways;
    same guard here rather than betting on one."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def detail_str(value: Any, limit: int) -> str:
    """Render an event's jsonb detail for display without double-encoding a
    value that arrived already serialised."""
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, default=str)[:limit]


async def q(sql: str, **params):
    async with engine().connect() as c:
        return (await c.execute(text(sql), params)).mappings().all()


def ctx(request: Request, page: str, **kw) -> dict:
    return {"page": page, "dry_run": settings.dry_run, **kw}


async def _stats() -> list[dict]:
    rows = await q("""
      SELECT
        (SELECT count(*) FROM app.job_groups)                                   AS groups,
        (SELECT count(*) FROM app.job_groups WHERE status='READY')              AS ready,
        (SELECT count(*) FROM app.job_groups WHERE status='REVIEW_REQUIRED')    AS review,
        (SELECT count(*) FROM app.postings)                                     AS postings,
        (SELECT count(*) FROM app.job_groups
          WHERE first_seen_at > now() - interval '24 hours')                    AS today,
        (SELECT count(*) FROM app.work_items WHERE state IN ('PENDING','RUNNING')) AS queued
    """)
    r = rows[0]
    labels = [("groups", "job groups"), ("ready", "ready"), ("review", "needs review"),
              ("postings", "postings"), ("today", "new 24h"), ("queued", "queued")]
    return [{"label": lab, "value": r[k]} for k, lab in labels]


async def _outbox() -> dict:
    """Architecture 7: the dashboard shows that the webhook is configured and
    the last-send status, never the URL itself."""
    rows = await q("""
      SELECT
        (SELECT count(*) FROM app.notifications WHERE state='PENDING') AS pending,
        (SELECT count(*) FROM app.notifications WHERE state='SENT')    AS sent,
        (SELECT count(*) FROM app.notifications WHERE state='DEAD')    AS dead,
        (SELECT max(sent_at) FROM app.notifications WHERE state='SENT') AS last_sent
    """)
    r = dict(rows[0])
    r["last_sent_sydney"] = syd(r.pop("last_sent"))
    return r


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    now = datetime.now(timezone.utc)
    hb = [{**dict(h), "last_beat_sydney": syd(h["last_beat"]),
           "age_s": int((now - h["last_beat"]).total_seconds())}
          for h in await q("SELECT worker, last_beat FROM app.heartbeats ORDER BY worker")]
    src = await q("""
        SELECT s.key, s.circuit_state, s.consecutive_failures,
               (SELECT count(*) FROM app.postings p WHERE p.source_id = s.id) AS postings
          FROM app.sources s WHERE s.enabled ORDER BY s.key""")
    return templates.TemplateResponse(
        request, "overview.html",
        ctx(request, "overview", stats=await _stats(), heartbeats=hb, sources=src,
            outbox=await _outbox()))


@router.get("/partials/stats", response_class=HTMLResponse)
async def partial_stats(request: Request):
    return templates.TemplateResponse(
        request, "_stats.html", ctx(request, "overview", stats=await _stats()))


JOBS_SQL = """
SELECT g.id, g.title, g.status, g.screen_reason, g.location_class,
       c.name AS company, p.posted_at, p.posted_at_confidence
  FROM app.job_groups g
  LEFT JOIN app.companies c ON c.id = g.company_id
  LEFT JOIN app.postings p ON p.id = g.canonical_posting_id
 WHERE (:status = '' OR g.status = :status)
   AND (:query = '' OR g.title ILIKE '%' || :query || '%')
 ORDER BY p.posted_at DESC NULLS LAST, g.id DESC
 LIMIT 100"""


async def _jobs(status: str, query: str):
    rows = await q(JOBS_SQL, status=status or "", query=query or "")
    return [{**dict(r), "posted_sydney": syd(r["posted_at"])} for r in rows]


# The query-string parameter is named `q` for the form, but bound to `query`
# here so it cannot shadow the module-level q() helper.
@router.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, status: str = "", query: str = ""):
    return templates.TemplateResponse(
        request, "jobs.html",
        ctx(request, "jobs", jobs=await _jobs(status, query), status=status, query=query))


@router.get("/partials/jobs", response_class=HTMLResponse)
async def partial_jobs(request: Request, status: str = "", query: str = ""):
    return templates.TemplateResponse(
        request, "_jobs.html", ctx(request, "jobs", jobs=await _jobs(status, query)))


@router.get("/jobs/{gid}", response_class=HTMLResponse)
async def job_detail(request: Request, gid: int):
    job = await q("""
        SELECT g.id, g.title, g.status, g.screen_reason, g.location_class,
               c.name AS company, p.posted_at, p.posted_at_confidence,
               p.description, p.salary
          FROM app.job_groups g
          LEFT JOIN app.companies c ON c.id = g.company_id
          LEFT JOIN app.postings p ON p.id = g.canonical_posting_id
         WHERE g.id = :g""", g=gid)
    if not job:
        return HTMLResponse("not found", status_code=404)
    j = {**dict(job[0]), "posted_sydney": syd(job[0]["posted_at"])}
    postings = await q("""
        SELECT s.key AS source, gp.match_layer, gp.probable_duplicate, p.canonical_url
          FROM app.job_group_postings gp
          JOIN app.postings p ON p.id = gp.posting_id
          JOIN app.sources s ON s.id = p.source_id
         WHERE gp.job_group_id = :g ORDER BY gp.match_layer""", g=gid)
    wr = await q("SELECT classification, clause_verbatim FROM app.work_right_assessments "
                 "WHERE job_group_id=:g ORDER BY created_at DESC LIMIT 1", g=gid)
    cl = await q("SELECT strength, clause_verbatim FROM app.clearance_assessments "
                 "WHERE job_group_id=:g ORDER BY created_at DESC LIMIT 1", g=gid)
    ev = [{**dict(e), "at_sydney": syd(e["at"]), "detail": detail_str(e["detail"], 200)}
          for e in await q("SELECT at, actor, action, detail FROM app.events "
                           "WHERE entity_type='job_group' AND entity_id=:g "
                           "ORDER BY at DESC LIMIT 50", g=gid)]
    return templates.TemplateResponse(
        request, "job_detail.html",
        ctx(request, "jobs", job=j, postings=postings,
            work_rights=wr[0] if wr else None, clearance=cl[0] if cl else None,
            salary=as_dict(j.get("salary")), events=ev, description=j.get("description")))


async def _pipeline():
    cols = await q("""
        SELECT status AS label, count(*) AS count FROM app.job_groups
         GROUP BY 1 ORDER BY 2 DESC""")
    work = [{**dict(w), "oldest_sydney": syd(w["oldest"])} for w in await q("""
        SELECT kind, state, count(*) AS count, min(created_at) AS oldest
          FROM app.work_items GROUP BY 1,2 ORDER BY 3 DESC""")]
    return cols, work


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline(request: Request):
    cols, work = await _pipeline()
    return templates.TemplateResponse(
        request, "pipeline.html", ctx(request, "pipeline", columns=cols, work=work))


@router.get("/partials/pipeline", response_class=HTMLResponse)
async def partial_pipeline(request: Request):
    cols, work = await _pipeline()
    return templates.TemplateResponse(
        request, "_pipeline.html", ctx(request, "pipeline", columns=cols, work=work))


async def _events():
    return [{**dict(e), "at_sydney": syd(e["at"]), "detail": detail_str(e["detail"], 160)}
            for e in await q("SELECT at, entity_type, entity_id, actor, action, detail "
                             "FROM app.events ORDER BY at DESC LIMIT 200")]


@router.get("/events", response_class=HTMLResponse)
async def events(request: Request):
    return templates.TemplateResponse(
        request, "events.html", ctx(request, "events", events=await _events()))


@router.get("/partials/events", response_class=HTMLResponse)
async def partial_events(request: Request):
    # _events.html, not events.html: the poll swaps outerHTML on the table.
    return templates.TemplateResponse(
        request, "_events.html", ctx(request, "events", events=await _events()))
