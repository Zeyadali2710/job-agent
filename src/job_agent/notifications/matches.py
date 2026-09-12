"""HIGH_MATCH_JOB producer. Architecture 7: on, score >= 80, full card.

Reads app.current_job_scores rather than app.job_scores: scores are append-only
per version, so the raw table holds every version a job has ever had and a
naive read would notify on a stale one.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.ai.client import decode_jsonb
from job_agent.pipeline.score import why_and_concerns

DEFAULT_MIN_SCORE = 80

CANDIDATES = text("""
SELECT s.id AS score_id, s.score, g.id AS gid, g.title,
       c.name AS company, g.location_class, p.employment_type, p.posted_at,
       p.canonical_url, p.salary
  FROM app.current_job_scores s
  JOIN app.job_groups g ON g.id = s.job_group_id
  LEFT JOIN app.companies c ON c.id = g.company_id
  LEFT JOIN app.postings p ON p.id = g.canonical_posting_id
 WHERE s.score >= :min AND g.status = 'READY'
   AND NOT EXISTS (SELECT 1 FROM app.notifications n
                    WHERE n.dedupe_key = 'high_match:' || g.id::text)
 ORDER BY s.score DESC
 LIMIT :n
""")

FEATURES = text("""
SELECT feature, weight, value, contribution, evidence
  FROM app.score_features WHERE score_id = :s
""")

INSERT_NOTIFICATION = text("""
INSERT INTO app.notifications (kind, dedupe_key, payload)
VALUES ('HIGH_MATCH_JOB', :key, CAST(:payload AS jsonb))
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
RETURNING id
""")

MIN_SCORE_SETTING = text(
    "SELECT value FROM app.settings WHERE key = 'notify.high_match_min_score'")


def salary_text(salary) -> str | None:
    s = decode_jsonb(salary) or {}
    if not isinstance(s, dict) or s.get("confidence") == "ABSENT":
        return None
    lo, hi = s.get("base_salary_min"), s.get("base_salary_max")
    if not lo:
        return None
    base = f"{int(lo):,}" + (f"-{int(hi):,}" if hi else "")
    pkg = s.get("total_package_min")
    return f"${base}" + (f" (package ${int(pkg):,})" if pkg else "")


async def notify_high_matches(limit: int = 20) -> dict:
    """One card per job group, ever -- the dedupe_key is the group id with no
    date in it, so a re-score cannot re-notify a job you have already seen."""
    from job_agent.core.db import tx

    counts = {"queued": 0}
    async with tx() as conn:
        raw = (await conn.execute(MIN_SCORE_SETTING)).scalar()
        min_score = float(decode_jsonb(raw) or DEFAULT_MIN_SCORE)
        rows = (await conn.execute(
            CANDIDATES, {"min": min_score, "n": limit})).mappings().all()

        for r in rows:
            features = [dict(f) for f in (await conn.execute(
                FEATURES, {"s": r["score_id"]})).mappings().all()]
            why, concerns = why_and_concerns(features)
            payload = {
                "title": r["title"],
                "job_group_id": r["gid"],
                "company": r["company"],
                "location_class": r["location_class"],
                "employment_type": r["employment_type"],
                "salary_text": salary_text(r["salary"]),
                "posted_sydney": r["posted_at"].isoformat() if r["posted_at"] else None,
                "score": float(r["score"]),
                "why": why,
                "concerns": concerns,
                "url": r["canonical_url"],
            }
            nid = (await conn.execute(INSERT_NOTIFICATION, {
                "key": f"high_match:{r['gid']}",
                "payload": json.dumps(payload)})).scalar()
            if nid:
                counts["queued"] += 1
    return counts
