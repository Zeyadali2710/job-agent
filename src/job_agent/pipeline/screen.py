from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.screening import clearance, grad, location, salary, seniority


async def _profile(conn) -> dict:
    row = (await conn.execute(text("""
        SELECT f.kind, f.data FROM app.profile_facts f
          JOIN app.profile_versions v ON v.id = f.profile_version_id
         WHERE v.is_active AND f.kind IN ('work_rights','preferences')"""))).all()
    out = {}
    for r in row:
        out[r.kind] = r.data if isinstance(r.data, dict) else json.loads(r.data)
    return out


def decide(prefs: dict, *, wr_class: str, cl_block: bool, sen_class: str,
           loc_class: str, conflict: bool, sal: dict) -> tuple[str, str | None]:
    """The hard screen. Pure -- no DB, no I/O -- so it is testable directly.

    Order is by certainty: a hard incompatibility outranks a preference, and
    a preference outranks an ambiguity. Returns (status, screen_reason).
    """
    # ---- hard gates, in order of certainty ----
    if wr_class == "INCOMPATIBLE":
        return "SCREENED_OUT", "WORK_RIGHTS_INCOMPATIBLE"
    if cl_block:
        return "SCREENED_OUT", "CLEARANCE_CITIZENSHIP_CONFLICT"
    if sen_class in ("SENIOR", "LEAD"):
        return "SCREENED_OUT", "SENIORITY_TOO_HIGH"
    accepted = set(prefs.get("location_classes_accepted", []))
    if loc_class == "UNKNOWN":
        return "REVIEW_REQUIRED", "LOCATION_UNKNOWN"
    if accepted and loc_class not in accepted:
        if loc_class == "OTHER_CAPITAL" and prefs.get("allow_relocation"):
            pass
        else:
            return "SCREENED_OUT", f"LOCATION_{loc_class}"
    if conflict:
        return "REVIEW_REQUIRED", "SENIORITY_CONFLICT"
    if prefs.get("salary_is_mandatory") and sal["confidence"] == "ABSENT":
        return "SCREENED_OUT", "SALARY_NOT_STATED"
    if (sal["total_package_min"] or 0) and prefs.get("salary_min") \
            and sal["total_package_min"] < prefs["salary_min"] * 0.9:
        return "SCREENED_OUT", "SALARY_BELOW_MINIMUM"
    # Architecture 6: "Never auto-reject for ambiguity ... a large share of AU
    # ads simply omit the clause." UNCERTAIN means no work-rights language was
    # found at all, which for a citizen profile is the normal case, not a
    # concern. The assessment row is already written above, so the concern is
    # recorded and renders on the dashboard either way.
    if wr_class == "UNCERTAIN" and prefs.get("uncertain_policy") == "review":
        return "REVIEW_REQUIRED", "WORK_RIGHTS_UNCERTAIN"
    return "READY", None


async def screen_group(conn, gid: int, prof: dict) -> tuple[str, str | None]:
    p = (await conn.execute(text("""
        SELECT p.title, p.description, p.location_raw, p.employment_type
          FROM app.job_groups g
          JOIN app.postings p ON p.id = g.canonical_posting_id
         WHERE g.id = :g"""), {"g": gid})).first()
    if p is None:
        return "SCREENED_OUT", "NO_CANONICAL_POSTING"

    wr = prof.get("work_rights", {})
    prefs = prof.get("preferences", {})
    blob = f"{p.title}\n{p.description or ''}"

    loc_class, days = location.classify(p.location_raw, p.description)
    sen_class, min_years, conflict = seniority.classify(p.title, p.description)
    sal = salary.parse(p.description)
    wr_class, wr_clause = work_rights_classify(blob, wr.get("status", "CITIZEN"))
    cl_detected, cl_strength, cl_clause, cl_block = clearance.classify(
        blob, wr.get("citizenship", "AU"))

    await conn.execute(text("""
        UPDATE app.postings SET location_class=:l, days_in_office=:d,
               seniority_class=:s, min_years_experience=:y, salary=CAST(:sal AS jsonb)
         WHERE id = (SELECT canonical_posting_id FROM app.job_groups WHERE id=:g)"""),
        {"l": loc_class, "d": days, "s": sen_class, "y": min_years,
         "sal": json.dumps(sal), "g": gid})

    # job_groups.location_class is a denormalised copy of the canonical
    # posting's, and the dashboard reads the group's. Without this the two
    # drift apart again the moment a re-screen changes the classification.
    #
    # is_grad_program and intake_year are set here too: architecture 6.3 specs
    # them, nothing had ever written them, and the scorer's 10-point
    # grad_program feature was therefore dead for every job in the corpus.
    is_grad, intake_year = grad.detect(p.title, p.description)
    await conn.execute(text(
        "UPDATE app.job_groups SET location_class=:l, is_grad_program=:gp, "
        "intake_year=:iy WHERE id=:g"),
        {"l": loc_class, "gp": is_grad, "iy": intake_year, "g": gid})

    await conn.execute(text("""
        INSERT INTO app.work_right_assessments (job_group_id, classification, clause_verbatim, decided_by)
        VALUES (:g,:c,:v,'RULE')"""), {"g": gid, "c": wr_class, "v": wr_clause})
    if cl_detected:
        await conn.execute(text("""
            INSERT INTO app.clearance_assessments (job_group_id, detected, strength, clause_verbatim, decided_by)
            VALUES (:g,:d,:s,:v,'RULE')"""),
            {"g": gid, "d": cl_detected, "s": cl_strength, "v": cl_clause})

    return decide(prefs, wr_class=wr_class, cl_block=cl_block,
                  sen_class=sen_class, loc_class=loc_class,
                  conflict=conflict, sal=sal)


def work_rights_classify(blob: str, status: str):
    from job_agent.screening.work_rights import classify
    return classify(blob, status)


async def screen_pending(limit: int = 200) -> dict:
    counts = {"ready": 0, "screened_out": 0, "review": 0}
    async with tx() as conn:
        prof = await _profile(conn)
        gids = [r.id for r in (await conn.execute(text(
            "SELECT id FROM app.job_groups WHERE status='DISCOVERED' ORDER BY id LIMIT :n"),
            {"n": limit})).all()]
    for gid in gids:
        async with tx() as conn:
            status, reason = await screen_group(conn, gid, prof)
            await conn.execute(text(
                "UPDATE app.job_groups SET status=:s, screen_reason=:r WHERE id=:g"),
                {"s": status, "r": reason, "g": gid})
            await conn.execute(text(
                "INSERT INTO app.events (entity_type, entity_id, actor, action, detail) "
                "VALUES ('job_group', :g, 'screen', 'screened', CAST(:d AS jsonb))"),
                {"g": gid, "d": json.dumps({"status": status, "reason": reason})})
        counts["ready" if status == "READY" else
               "review" if status == "REVIEW_REQUIRED" else "screened_out"] += 1
    return counts
