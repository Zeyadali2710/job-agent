"""Create DRAFT applications for jobs worth drafting for.

Phase 13 replaces the score threshold here with the rule engine in
app.application_rules. Until then this is deliberately the dumbest thing that
works: the top-scoring READY groups, capped per run, one application each.

Drafting is not applying. Nothing here approaches DRY_RUN, the daily cap or a
submit path -- it creates a row whose whole purpose is to hang documents off.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.core.queue import enqueue

DEFAULT_MIN_SCORE = 45.0
DEFAULT_MAX_PER_RUN = 5

SETTINGS = text("SELECT key, value FROM app.settings WHERE key = ANY(:keys)")

# The partial unique index (applications_one_live_per_group) already makes a
# second live application impossible; this only avoids attempting one.
CANDIDATES = text("""
SELECT g.id AS gid, s.score
  FROM app.job_groups g
  JOIN app.current_job_scores s ON s.job_group_id = g.id
 WHERE g.status = 'READY'
   AND s.score >= :min
   AND NOT EXISTS (SELECT 1 FROM app.applications a
                    WHERE a.job_group_id = g.id
                      AND a.status NOT IN ('WITHDRAWN','FAILED_PERMANENT'))
 ORDER BY s.score DESC, g.id
 LIMIT :n
""")

ACTIVE_PROFILE = text("SELECT id FROM app.profile_versions WHERE is_active")

INSERT_APPLICATION = text("""
INSERT INTO app.applications (job_group_id, status, profile_version_id)
VALUES (:g, 'DRAFT', :pv)
ON CONFLICT (job_group_id) WHERE status NOT IN ('WITHDRAWN','FAILED_PERMANENT')
DO NOTHING
RETURNING id
""")

INSERT_HISTORY = text("""
INSERT INTO app.application_status_history (application_id, from_status, to_status, actor, reason)
VALUES (:a, NULL, 'DRAFT', 'draft', :reason)
""")

INSERT_EVENT = text("""
INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('application', :a, 'draft', 'created', CAST(:d AS jsonb))
""")


async def _config(conn) -> tuple[float, int]:
    keys = ["generate.min_score", "generate.max_drafts_per_run"]
    rows = dict((r.key, r.value) for r in (await conn.execute(
        SETTINGS, {"keys": keys})).all())

    def num(key, default):
        v = rows.get(key)
        if isinstance(v, str):          # jsonb scalars arrive as strings
            v = json.loads(v)
        return default if v is None else v

    return (float(num("generate.min_score", DEFAULT_MIN_SCORE)),
            int(num("generate.max_drafts_per_run", DEFAULT_MAX_PER_RUN)))


async def draft_applications(limit: int | None = None) -> dict:
    counts = {"drafted": 0, "skipped": 0}
    async with tx() as conn:
        min_score, max_per_run = await _config(conn)
        n = limit or max_per_run
        pv = (await conn.execute(ACTIVE_PROFILE)).scalar()
        rows = (await conn.execute(
            CANDIDATES, {"min": min_score, "n": n})).mappings().all()

    for row in rows:
        async with tx() as conn:
            app_id = (await conn.execute(
                INSERT_APPLICATION, {"g": row["gid"], "pv": pv})).scalar()
            if app_id is None:          # raced with another worker
                counts["skipped"] += 1
                continue
            reason = f"score {float(row['score']):.1f} >= {min_score:.1f}"
            await conn.execute(INSERT_HISTORY, {"a": app_id, "reason": reason})
            await conn.execute(INSERT_EVENT, {
                "a": app_id,
                "d": json.dumps({"job_group_id": row["gid"],
                                 "score": float(row["score"]),
                                 "profile_version_id": pv})})
            await enqueue(conn, "generate", {"application_id": app_id},
                          dedupe_key=f"generate:{app_id}")
        counts["drafted"] += 1
    return counts
