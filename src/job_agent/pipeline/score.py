"""Deterministic scoring. Architecture 14 step 6: a weighted score reproducible
from the database alone, plus a bounded LLM adjustment of at most +/-10.

Nothing here calls a model. The LLM may nudge the result later, and
job_scores.llm_adjustment carries a CHECK constraint enforcing the bound, so a
prompt injection cannot turn a 40 into a 90.

The weights encode *your* preferences, and they come from preferences.yaml
rather than from anyone's opinion about what a good job looks like. They live in
app.settings so tuning them does not need a deploy.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from sqlalchemy import text

# 1.1.0: grad_program became a live feature. The scorer code is unchanged, but
# its inputs are not -- nothing had ever set job_groups.is_grad_program, so the
# 10-point feature scored zero for the entire corpus. Bumping re-scores it.
# 1.2.0: skill_overlap's denominator gained a floor and an empty extraction
# stopped being neutral. The weights and the profile are hashed into the
# version, but the scoring logic is not, so changing it needs this bump or
# every existing score stays and the re-score reports scored: 0.
SCORER_VERSION = "1.2.0"

# The positive weights sum to exactly 100, grad_program included. An earlier
# cut treated it as a bonus on top of 100, which meant a strong job hit the
# 0..100 clamp and every penalty below it became invisible -- two jobs that
# differed by a real concern both scored 100. Nothing may exceed 100 before
# clamping, or the clamp silently destroys discrimination at the top of the
# list, which is the only part of the list you read.
DEFAULT_WEIGHTS: dict[str, float] = {
    "seniority_match": 28,
    "location_rank": 18,
    "salary_vs_target": 14,
    "skill_overlap": 14,
    "employment_type_match": 8,
    "freshness": 8,
    "grad_program": 10,
    "work_rights_uncertain": -5,
}

# HIGH is 80 to line up with the HIGH_MATCH_JOB notification threshold in
# architecture 7. The card's colour bands (85/70) are a separate, finer scale
# applied to the final score in cards.py.
BAND_HIGH = 80
BAND_MEDIUM = 60

FRESHNESS_DAYS = 30
# Neutral, not zero. A silent job ad is not a worse job ad -- the same reasoning
# that stopped UNCERTAIN work rights from holding a job in review.
UNKNOWN = 0.5


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _seniority(job: Mapping, prefs: Mapping) -> tuple[float, str]:
    targets = list(prefs.get("seniority_targets") or [])
    cls = job.get("seniority_class")
    if not cls or cls == "UNKNOWN":
        return UNKNOWN, "seniority not stated in the title"
    if cls in targets:
        return 1.0, f"{cls} is in your targets {targets}"
    return 0.0, f"{cls} is outside your targets {targets}"


def _location(job: Mapping, prefs: Mapping) -> tuple[float, str]:
    accepted = list(prefs.get("location_classes_accepted") or [])
    cls = job.get("location_class")
    if cls in accepted:
        idx = accepted.index(cls)
        # Linear 1.0 -> 0.5 across the list: every accepted location is good,
        # the order just says which you prefer.
        value = 1.0 - 0.5 * (idx / max(1, len(accepted) - 1))
        return value, f"{cls} is preference {idx + 1} of {len(accepted)}"
    if not cls or cls == "UNKNOWN":
        return UNKNOWN, "location could not be classified"
    return 0.0, f"{cls} is not in your accepted locations"


def _salary(job: Mapping, prefs: Mapping) -> tuple[float, str]:
    target = prefs.get("salary_target") or 0
    sal = job.get("salary") or {}
    total = sal.get("total_package_min")
    if not target or not total:
        return UNKNOWN, "no salary stated"
    value = _clamp(total / target)
    return value, f"package {int(total):,} against target {int(target):,}"


def _skills(job: Mapping, prefs: Mapping) -> tuple[float, str]:
    """Needs the LLM extraction from job_analyses. Neutral before enrichment,
    so the scorer is useful on its own and improves when enrichment lands."""
    job_skills = {s.lower() for s in (job.get("job_skills") or [])}
    mine = {s.lower() for s in (job.get("profile_skills") or [])}
    if not job_skills:
        # An analysis that ran and named nothing is not the same as no analysis.
        # Both used to return the neutral 0.5, which put an Inventory Clerk ad
        # above a security role that genuinely matched 4 of 20.
        if job.get("analysed"):
            return 0.0, "enrichment named no technologies for this role"
        return UNKNOWN, "requirements not yet extracted"
    hit = job_skills & mine
    # Capped at 8: a job listing 20 skills should not need all 20 matched.
    # ponytail: floored at 4. An ad naming fewer than four technologies has not
    # described a stack, so matching one of them is not evidence of fit -- an
    # ad listing a single skill scored a perfect 1.00 and outranked a security
    # role matching 5 of 19. Raise the floor if sparse ads still rank high.
    value = _clamp(len(hit) / min(max(len(job_skills), 4), 8))
    shown = ", ".join(sorted(hit)[:5]) or "none"
    return value, f"{len(hit)} of {len(job_skills)} required skills matched: {shown}"


def _employment(job: Mapping, prefs: Mapping) -> tuple[float, str]:
    accepted = list(prefs.get("employment_types") or [])
    et = job.get("employment_type")
    if not et:
        return UNKNOWN, "employment type not stated"
    if et in accepted:
        return 1.0, f"{et} is in {accepted}"
    return 0.0, f"{et} is not in {accepted}"


def _freshness(job: Mapping, prefs: Mapping, now: datetime) -> tuple[float, str]:
    """Architecture 0.2: freshness is a scoring input, never a hard gate."""
    posted = job.get("posted_at")
    if not posted:
        return UNKNOWN, "posting date unknown"
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    days = (now - posted).total_seconds() / 86400
    return _clamp(1.0 - days / FRESHNESS_DAYS), f"posted {int(days)} days ago"


def _grad(job: Mapping, prefs: Mapping) -> tuple[float, str]:
    if job.get("is_grad_program"):
        return 1.0, "identified as a graduate program"
    return 0.0, "not a graduate program"


def _wr_uncertain(job: Mapping, prefs: Mapping) -> tuple[float, str]:
    """A negative weight. UNCERTAIN no longer blocks the job (architecture 6
    says never auto-reject for ambiguity), so it lands here instead -- a small
    push down the list and a line in the card's concerns."""
    if job.get("work_rights_class") == "UNCERTAIN":
        return 1.0, "the ad says nothing about work rights"
    return 0.0, "work rights addressed"


def score(job: Mapping, prefs: Mapping, weights: Mapping | None = None,
          now: datetime | None = None) -> tuple[float, str, list[dict]]:
    """Return (base_score, band, features).

    features are ready to insert as score_features rows, each carrying the
    evidence string the Discord card and the dashboard render -- so "why it
    matches" is generated from data rather than written as prose.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    now = now or datetime.now(timezone.utc)

    computed = [
        ("seniority_match", *_seniority(job, prefs)),
        ("location_rank", *_location(job, prefs)),
        ("salary_vs_target", *_salary(job, prefs)),
        ("skill_overlap", *_skills(job, prefs)),
        ("employment_type_match", *_employment(job, prefs)),
        ("freshness", *_freshness(job, prefs, now)),
        ("grad_program", *_grad(job, prefs)),
        ("work_rights_uncertain", *_wr_uncertain(job, prefs)),
    ]

    features = []
    total = 0.0
    for name, value, evidence in computed:
        weight = float(w.get(name, 0))
        contribution = weight * value
        total += contribution
        features.append({
            "feature": name,
            "weight": round(weight, 3),
            "value": round(value, 3),
            "contribution": round(contribution, 3),
            "evidence": evidence,
        })

    base = round(_clamp(total, 0.0, 100.0), 2)
    band = "HIGH" if base >= BAND_HIGH else "MEDIUM" if base >= BAND_MEDIUM else "LOW"
    return base, band, features


def final_score(base: float, llm_adjustment: float = 0.0) -> float:
    """The LLM may move the score by at most +/-10, and never outside 0..100.
    job_scores has CHECK constraints for both; this keeps the Python side from
    ever offering a value the database would reject."""
    adj = max(-10.0, min(10.0, float(llm_adjustment)))
    return round(_clamp(base + adj, 0.0, 100.0), 2)


def why_and_concerns(features: Sequence[Mapping], n: int = 5) -> tuple[list[str], list[str]]:
    """Split the evidence into the card's two lists. Positive contributions are
    reasons, zero-or-negative ones are concerns, each ordered by how much they
    actually moved the score."""
    ranked = sorted(features, key=lambda f: f["contribution"], reverse=True)
    why = [f["evidence"] for f in ranked if f["contribution"] > 0][:n]
    concerns = [f["evidence"] for f in reversed(ranked) if f["contribution"] <= 0][:n]
    return why, concerns


# ---------------------------------------------------------------- persistence

def effective_version(weights: Mapping, profile_sha: str | None = None) -> str:
    """SCORER_VERSION plus a hash of every input that is not the job itself.

    job_scores has UNIQUE (job_group_id, scorer_version) and ja_worker has no
    DELETE on score_features, so a row cannot be rewritten in place -- which is
    the schema's intent: a score is a record of what one scorer version
    concluded. But the weights live in app.settings so they can be tuned
    without a deploy, and tuning them without changing the version would leave
    every existing score stale and un-recomputable. Folding the weights into
    the version means editing a weight re-scores the corpus automatically.

    The profile is an input by exactly the same argument and was missing from
    it: skill_overlap reads your skills and the preferences drive the seniority
    and location features, so `make profile-load` invalidates every score.
    Without the sha in here a re-score after loading a new profile reports
    "scored: 0" and quietly leaves the corpus ranked against the old you.
    """
    import hashlib
    import json as _json
    blob = _json.dumps(
        {"profile": profile_sha,
         "weights": {k: float(v) for k, v in sorted(weights.items())}},
        separators=(",", ":"))
    return f"{SCORER_VERSION}+{hashlib.sha256(blob.encode()).hexdigest()[:8]}"


ACTIVE_PROFILE_SHA = text(
    "SELECT content_sha256 FROM app.profile_versions WHERE is_active")


# Needs-scoring, not merely never-scored. Enrichment fills job_analyses.skills
# -- the input to skill_overlap -- and necessarily lands after the first score,
# because every free stage runs before a token is spent. A job whose analysis
# is newer than its score is stale and must be recomputed.
UNSCORED = text("""
SELECT g.id
  FROM app.job_groups g
  LEFT JOIN app.job_scores s
         ON s.job_group_id = g.id AND s.scorer_version = :ver
 WHERE g.status IN ('READY', 'REVIEW_REQUIRED')
   AND (s.id IS NULL
        OR EXISTS (SELECT 1 FROM app.job_analyses a
                    WHERE a.job_group_id = g.id AND a.created_at > s.created_at))
 ORDER BY g.id
 LIMIT :n
""")

JOB_ROW = text("""
SELECT g.id, g.is_grad_program,
       p.seniority_class, p.location_class, p.employment_type, p.salary, p.posted_at,
       (SELECT w.classification FROM app.work_right_assessments w
         WHERE w.job_group_id = g.id ORDER BY w.created_at DESC LIMIT 1) AS work_rights_class,
       (SELECT a.skills FROM app.job_analyses a
         WHERE a.job_group_id = g.id ORDER BY a.analysis_version DESC LIMIT 1) AS job_skills
  FROM app.job_groups g
  JOIN app.postings p ON p.id = g.canonical_posting_id
 WHERE g.id = :g
""")

PROFILE_SKILLS = text("""
SELECT f.body FROM app.profile_facts f
  JOIN app.profile_versions v ON v.id = f.profile_version_id
 WHERE v.is_active AND f.kind = 'skill'
""")

# Updated in place for the same version, so recomputing after enrichment
# replaces the stale score rather than being silently dropped. created_at moves
# too, which is what current_job_scores orders by and what makes the
# needs-scoring check above terminate.
INSERT_SCORE = text("""
INSERT INTO app.job_scores
  (job_group_id, base_score, llm_adjustment, score, band, scorer_version)
VALUES (:g, :base, 0, :score, :band, :ver)
ON CONFLICT (job_group_id, scorer_version) DO UPDATE
  SET base_score = EXCLUDED.base_score,
      score      = EXCLUDED.score,
      band       = EXCLUDED.band,
      created_at = now()
RETURNING id
""")

# The evidence belongs to the score; replacing one replaces the other.
# ja_worker gets DELETE on score_features in 0009 for exactly this.
CLEAR_FEATURES = text("DELETE FROM app.score_features WHERE score_id = :s")

INSERT_FEATURE = text("""
INSERT INTO app.score_features (score_id, feature, weight, value, contribution, evidence)
VALUES (:s, :f, :w, :v, :c, :e)
""")


def _as_list(value) -> list:
    """jsonb arrives as list or str depending on driver codec registration --
    the same guard screen.py and web/routes.py already carry."""
    import json as _json
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            loaded = _json.loads(value)
        except ValueError:
            return []
        return loaded if isinstance(loaded, list) else []
    return []


def _skill_names(rows) -> list[str]:
    """job_analyses.skills is a list of either plain strings or objects with a
    name; accept both so a prompt-format change does not silently zero the
    skill_overlap feature."""
    out = []
    for item in rows:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, Mapping):
            name = item.get("name") or item.get("skill")
            if name:
                out.append(str(name))
    return out


async def score_pending(limit: int = 200) -> dict:
    """Score every READY/REVIEW_REQUIRED group that has no score at the current
    effective version. Deterministic and free -- no model is called."""
    import json as _json

    from job_agent.ai.client import load_config
    from job_agent.core.db import tx
    from job_agent.pipeline.screen import _profile

    counts = {"scored": 0, "high": 0, "skipped": 0}
    async with tx() as conn:
        cfg = await load_config(conn)
        weights = {**DEFAULT_WEIGHTS, **(cfg.get("score.weights") or {})}
        ver = effective_version(
            weights, (await conn.execute(ACTIVE_PROFILE_SHA)).scalar())
        prof = await _profile(conn)
        prefs = prof.get("preferences", {})
        profile_skills = [r.body for r in (await conn.execute(PROFILE_SKILLS)).all()]
        gids = [r.id for r in (await conn.execute(UNSCORED, {"ver": ver, "n": limit})).all()]

    for gid in gids:
        async with tx() as conn:
            row = (await conn.execute(JOB_ROW, {"g": gid})).mappings().first()
            if row is None:
                counts["skipped"] += 1
                continue
            salary = row["salary"]
            if isinstance(salary, str):
                salary = _json.loads(salary or "{}")
            job = {
                "seniority_class": row["seniority_class"],
                "location_class": row["location_class"],
                "employment_type": row["employment_type"],
                "salary": salary or {},
                "posted_at": row["posted_at"],
                "is_grad_program": row["is_grad_program"],
                "work_rights_class": row["work_rights_class"],
                "job_skills": _skill_names(_as_list(row["job_skills"])),
                # NULL only when no analysis row exists at all; an enriched ad
                # with nothing to name arrives as [], which is information.
                "analysed": row["job_skills"] is not None,
                "profile_skills": profile_skills,
            }
            base, band, features = score(job, prefs, weights)
            sid = (await conn.execute(INSERT_SCORE, {
                "g": gid, "base": base, "score": final_score(base), "band": band,
                "ver": ver})).scalar()
            if sid is None:          # raced with another worker; its rows stand
                counts["skipped"] += 1
                continue
            await conn.execute(CLEAR_FEATURES, {"s": sid})
            for f in features:
                await conn.execute(INSERT_FEATURE, {
                    "s": sid, "f": f["feature"], "w": f["weight"], "v": f["value"],
                    "c": f["contribution"], "e": f["evidence"]})
            counts["scored"] += 1
            if band == "HIGH":
                counts["high"] += 1
    return counts
