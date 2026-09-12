"""LLM enrichment of screen survivors. Architecture 6 volume table: roughly a
dozen a day, Haiku-class, only on jobs that already passed the hard screen.

The order of spend is the design. The hard screen kills most of the corpus for
free, scoring ranks what is left for free, and only survivors ever cost a
token. Nothing here runs before the free stages have had their say.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.ai import client as ai
from job_agent.ai.prompts import extract as prompt
from job_agent.core.config import get_settings
from job_agent.core.db import tx

ANALYSIS_VERSION = 1

UNENRICHED = text("""
SELECT g.id, g.title, c.name AS company, p.description
  FROM app.job_groups g
  JOIN app.postings p ON p.id = g.canonical_posting_id
  LEFT JOIN app.companies c ON c.id = g.company_id
 WHERE g.status IN ('READY', 'REVIEW_REQUIRED')
   AND p.description IS NOT NULL AND length(p.description) > 200
   AND NOT EXISTS (SELECT 1 FROM app.job_analyses a
                    WHERE a.job_group_id = g.id AND a.analysis_version = :ver)
 ORDER BY g.first_seen_at DESC
 LIMIT :n
""")

INSERT_ANALYSIS = text("""
INSERT INTO app.job_analyses
  (job_group_id, analysis_version, model, prompt_sha256, requirements, skills,
   grad_metadata)
VALUES (:g, :ver, :model, :sha, CAST(:reqs AS jsonb), CAST(:skills AS jsonb),
        CAST(:grad AS jsonb))
ON CONFLICT (job_group_id, analysis_version) DO NOTHING
""")

# Enrichment may revise what screening guessed from the title alone: the ad
# body can say "2027 intake" where the title said nothing.
UPDATE_GRAD = text("""
UPDATE app.job_groups
   SET is_grad_program = is_grad_program OR :gp,
       intake_year = coalesce(intake_year, :iy)
 WHERE id = :g
""")


async def enrich_pending(limit: int = 25) -> dict:
    """Extract requirements and skills for survivors that have no analysis.

    Returns counts. Stops cleanly -- not by raising -- when the budget is spent
    or ai.mode is off, because neither is an error: the deterministic pipeline
    carries on without enrichment and simply scores skill_overlap as neutral.
    """
    counts = {"enriched": 0, "skipped": 0, "failed": 0, "cost_aud": 0.0,
              "stopped": None}

    async with tx() as conn:
        cfg = await ai.load_config(conn)
        try:
            remaining = await ai.check_budget(conn, cfg)
        except (ai.AIDisabled, ai.BudgetExceeded) as stop:
            counts["stopped"] = str(stop)
            return counts
        fx = float(cfg.get("ai.fx_aud_per_usd", ai.DEFAULT_FX_AUD_PER_USD))
        rows = (await conn.execute(
            UNENRICHED, {"ver": ANALYSIS_VERSION, "n": limit})).mappings().all()

    if not rows:
        return counts

    api_key = get_settings().anthropic_api_key
    llm = ai.build_client(api_key)

    for row in rows:
        user = prompt.build_user(row["title"], row["company"], row["description"])
        sha = ai.payload_sha256({"system": prompt.SYSTEM, "user": user})
        try:
            data, usage = ai.call_json(
                llm, model=ai.HAIKU, system=prompt.SYSTEM, user=user,
                schema=prompt.SCHEMA, fx=fx, max_tokens=1500)
        except Exception as exc:
            async with tx() as conn:
                await ai.log_call(
                    conn, purpose="extract",
                    usage=ai.Usage(ai.HAIKU, 0, 0, 0.0), sha=sha,
                    entity_type="job_group", entity_id=row["id"],
                    ok=False, error=f"{type(exc).__name__}: {exc}")
            counts["failed"] += 1
            continue

        async with tx() as conn:
            await ai.log_call(conn, purpose="extract", usage=usage, sha=sha,
                              entity_type="job_group", entity_id=row["id"], ok=True)
            await conn.execute(INSERT_ANALYSIS, {
                "g": row["id"], "ver": ANALYSIS_VERSION, "model": usage.model,
                "sha": sha,
                "reqs": json.dumps(data.get("requirements") or []),
                "skills": json.dumps(data.get("skills") or []),
                "grad": json.dumps({
                    k: data.get(k) for k in
                    ("degree_requirements", "graduation_window_years",
                     "years_experience_min", "is_grad_program", "intake_year")
                }),
            })
            await conn.execute(UPDATE_GRAD, {
                "g": row["id"],
                "gp": bool(data.get("is_grad_program")),
                "iy": data.get("intake_year"),
            })
        counts["enriched"] += 1
        counts["cost_aud"] = round(counts["cost_aud"] + usage.cost_aud, 6)

        # Re-check between calls, not just once at the top: a long batch must
        # not run past the cap because the cap was only consulted before the
        # first item.
        remaining -= usage.cost_aud
        if remaining <= 0:
            counts["stopped"] = "monthly budget reached mid-batch"
            break

    return counts
