from __future__ import annotations

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.pipeline.dedupe import resolve


async def dedupe_pending(limit: int = 200) -> dict:
    counts = {"new_groups": 0, "merged": 0, "probable": 0}
    async with tx() as conn:
        ids = [r.id for r in (await conn.execute(text(
            "SELECT id FROM app.postings WHERE state = 'NORMALISED' ORDER BY id LIMIT :n"),
            {"n": limit})).all()]
    for pid in ids:
        async with tx() as conn:
            _, layer, probable = await resolve(conn, pid)
        if layer == 1:
            counts["new_groups"] += 1
        else:
            counts["merged"] += 1
        if probable:
            counts["probable"] += 1
    return counts
