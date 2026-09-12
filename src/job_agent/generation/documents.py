"""Draft the documents for an application. Runs in the worker, which holds the
API key; the renderer turns the result into a PDF and is the only thing that
ever touches contact details.

Two documents, two very different risk profiles:

- The resume is assembled deterministically from selected profile facts. Every
  string on it is a fact body copied verbatim, so it cannot contain a claim the
  profile does not already make.
- The cover letter is written by a model and is therefore guilty until the
  provenance validator says otherwise. A failure quarantines it and raises a
  notification. There is no regeneration loop -- architecture 9 is explicit
  that the draft is quarantined, not retried.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.ai import client as ai
from job_agent.ai.prompts import cover_letter as prompt
from job_agent.core.config import get_settings
from job_agent.core.db import tx
from job_agent.core.queue import enqueue
from job_agent.generation import provenance
from job_agent.generation.select import Fact, facts_by_id, select, selected_facts

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

PENDING = text("""
SELECT a.id AS app_id, a.job_group_id, a.profile_version_id,
       g.title, c.name AS company, p.description
  FROM app.applications a
  JOIN app.job_groups g ON g.id = a.job_group_id
  JOIN app.postings p ON p.id = g.canonical_posting_id
  LEFT JOIN app.companies c ON c.id = g.company_id
 WHERE a.status = 'DRAFT'
   AND NOT EXISTS (SELECT 1 FROM app.generated_documents d
                    WHERE d.application_id = a.id AND d.kind = 'COVER_LETTER')
 ORDER BY a.id
 LIMIT :n
""")

ONE_APPLICATION = text("""
SELECT a.id AS app_id, a.job_group_id, a.profile_version_id,
       g.title, c.name AS company, p.description
  FROM app.applications a
  JOIN app.job_groups g ON g.id = a.job_group_id
  JOIN app.postings p ON p.id = g.canonical_posting_id
  LEFT JOIN app.companies c ON c.id = g.company_id
 WHERE a.id = :a
""")

ANALYSIS = text("""
SELECT skills, requirements FROM app.job_analyses
 WHERE job_group_id = :g ORDER BY analysis_version DESC LIMIT 1
""")

FACTS = text("""
SELECT f.fact_id, f.kind, f.body, f.tags, f.data
  FROM app.profile_facts f
  JOIN app.profile_versions v ON v.id = f.profile_version_id
 WHERE v.is_active
 ORDER BY f.id
""")

UPSERT_DOCUMENT = text("""
INSERT INTO app.generated_documents
  (application_id, kind, profile_version_id, content, source_ids,
   validator_state, validator_findings, model)
VALUES (:a, :kind, :pv, CAST(:content AS jsonb), :sids, :state,
        CAST(:findings AS jsonb), :model)
ON CONFLICT (application_id, kind) DO UPDATE SET
  content = EXCLUDED.content, source_ids = EXCLUDED.source_ids,
  validator_state = EXCLUDED.validator_state,
  validator_findings = EXCLUDED.validator_findings,
  model = EXCLUDED.model, profile_version_id = EXCLUDED.profile_version_id,
  path = NULL, sha256 = NULL, created_at = now()
RETURNING id
""")

INSERT_EVENT = text("""
INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('application', :a, 'generate', :action, CAST(:d AS jsonb))
""")

QUARANTINE_NOTIFICATION = text("""
INSERT INTO app.notifications (kind, dedupe_key, payload)
VALUES ('MANUAL_INTERVENTION_REQUIRED', :key, CAST(:p AS jsonb))
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
""")


# ------------------------------------------------------------------ pure helpers

def fmt_date(value) -> str:
    """"2024-10" and "2024-10-01" both read as "Oct 2024". Anything else is
    passed through: a profile may legitimately say "2024" or "ongoing"."""
    s = str(value or "").strip()
    if len(s) >= 7 and s[4] == "-" and s[:4].isdigit() and s[5:7].isdigit():
        month = int(s[5:7])
        if 1 <= month <= 12:
            return f"{MONTHS[month - 1]} {s[:4]}"
    return s


def _entry(data, bullet_ids: set[str], *, text_key: str = "text") -> list[str]:
    return [b[text_key] for b in (data.get("bullets") or data.get("highlights") or ())
            if b.get("id") in bullet_ids]


def build_resume(facts, sel) -> dict:
    """Structured resume content. Every string is a fact body copied verbatim.

    A parent with no selected bullets is dropped entirely rather than printed
    as a heading with nothing under it -- except education, which earns its
    place on a graduate resume whether or not a highlight survived ranking.
    """
    index = {f.id: f for f in facts}
    chosen_exp, chosen_proj = set(sel.experience), set(sel.projects)
    chosen_edu = set(sel.education_highlights)

    education = []
    for fid in sel.education:
        d = dict(index[fid].data or {})
        education.append({
            "qualification": d.get("qualification", index[fid].body),
            "institution": d.get("institution", ""),
            "start": fmt_date(d.get("start")), "end": fmt_date(d.get("end")),
            "bullets": _entry(d, chosen_edu),
        })

    experience = []
    for f in facts:
        if f.kind != "experience":
            continue
        d = dict(f.data or {})
        bullets = _entry(d, chosen_exp)
        if bullets:
            experience.append({
                "title": d.get("title", ""), "organisation": d.get("organisation", ""),
                "start": fmt_date(d.get("start")), "end": fmt_date(d.get("end")),
                "bullets": bullets,
            })

    projects = []
    for f in facts:
        if f.kind != "project":
            continue
        d = dict(f.data or {})
        bullets = _entry(d, chosen_proj)
        if bullets:
            projects.append({
                "name": d.get("name", f.body),
                "start": fmt_date(d.get("start")), "end": fmt_date(d.get("end")),
                "bullets": bullets,
            })

    return {
        "education": education,
        "experience": experience,
        "projects": projects,
        "skills": [index[i].body for i in sel.skills if i in index],
    }


def parent_of(facts) -> dict[str, str]:
    """bullet id -> the engagement it belongs to, read from each parent's own
    bullet list rather than from an id prefix, so a profile that names its
    facts differently still maps correctly. Skills, education and STAR answers
    are left out: they are background, not engagements, and citing two of them
    alongside one role is not a merge."""
    out: dict[str, str] = {}
    for f in facts:
        if f.kind not in ("experience", "project"):
            continue
        label = (f.data or {}).get("organisation") or (f.data or {}).get("name") or f.id
        for b in (f.data or {}).get("bullets") or ():
            if b.get("id"):
                out[b["id"]] = str(label)
    return out


def parent_bodies(facts) -> dict[str, str]:
    """bullet id -> the full body of the record it belongs to.

    "Product Owner, ArmsOA" for an experience bullet, the project's name for a
    project bullet, "BSc Computer Science, UNSW" for an education highlight.
    The employer, the institution and the project name live there and nowhere
    else, so a letter that names the thing it is describing needs them.
    """
    out: dict[str, str] = {}
    for f in facts:
        if f.kind not in ("experience", "project", "education"):
            continue
        d = f.data or {}
        for b in (d.get("bullets") or d.get("highlights") or ()):
            if b.get("id"):
                out[b["id"]] = f.body
    return out


def job_text(title: str, company: str | None, description: str | None) -> str:
    """The advertisement as the validator sees it: everything the model was
    shown, so an entity it borrowed from the ad is not read as invented."""
    return f"{title}\n{company or ''}\n{description or ''}"


# ------------------------------------------------------------------ loading

def _facts_from_rows(rows) -> list[Fact]:
    # body is nullable in the schema; selection lowercases it unconditionally.
    return [Fact(id=r["fact_id"], kind=r["kind"], body=r["body"] or "",
                 tags=tuple(r["tags"] or ()),
                 data=ai.decode_jsonb(r["data"]) or {})
            for r in rows]


async def load_facts(conn) -> list[Fact]:
    return _facts_from_rows((await conn.execute(FACTS)).mappings().all())


async def _analysis(conn, gid: int) -> tuple[list, list]:
    row = (await conn.execute(ANALYSIS, {"g": gid})).mappings().first()
    if not row:
        return [], []
    return (ai.decode_jsonb(row["skills"]) or [],
            ai.decode_jsonb(row["requirements"]) or [])


# ------------------------------------------------------------------ the work

async def generate_pending(limit: int = 5, application_id: int | None = None) -> dict:
    """Draft resume content and a cover letter for DRAFT applications.

    Stops cleanly when the budget is spent or ai.mode is off: the resume is
    deterministic and still gets written, so an application without budget has
    a usable resume and no cover letter rather than nothing at all.
    """
    counts = {"resumes": 0, "letters": 0, "quarantined": 0, "failed": 0,
              "cost_aud": 0.0, "stopped": None}

    async with tx() as conn:
        cfg = await ai.load_config(conn)
        fx = float(cfg.get("ai.fx_aud_per_usd", ai.DEFAULT_FX_AUD_PER_USD))
        rows = (await conn.execute(
            ONE_APPLICATION, {"a": application_id}) if application_id
            else await conn.execute(PENDING, {"n": limit})).mappings().all()
        facts = await load_facts(conn)
        try:
            remaining = await ai.check_budget(conn, cfg)
        except (ai.AIDisabled, ai.BudgetExceeded) as stop:
            counts["stopped"] = str(stop)
            remaining = 0.0

    if not rows:
        return counts
    if not facts:
        counts["stopped"] = "no active profile version; run make profile-load"
        return counts

    all_facts = facts_by_id(facts)
    llm = ai.build_client(get_settings().anthropic_api_key) if remaining > 0 else None

    for row in rows:
        skills, requirements = [], []
        async with tx() as conn:
            skills, requirements = await _analysis(conn, row["job_group_id"])

        sel = select(facts, job_skills=skills, job_requirements=requirements,
                     job_title=row["title"])
        resume = build_resume(facts, sel)

        async with tx() as conn:
            await conn.execute(UPSERT_DOCUMENT, {
                "a": row["app_id"], "kind": "RESUME", "pv": row["profile_version_id"],
                "content": json.dumps(resume), "sids": list(sel.all_ids),
                # Assembled from fact bodies verbatim, so the entity scan would
                # be checking the facts against themselves.
                "state": provenance.PASSED, "findings": "[]", "model": None})
            await conn.execute(INSERT_EVENT, {
                "a": row["app_id"], "action": "resume_drafted",
                "d": json.dumps({"facts": len(sel.all_ids)})})
        counts["resumes"] += 1

        if llm is None or remaining <= 0:
            continue

        ad = job_text(row["title"], row["company"], row["description"])
        user = prompt.build_user(row["title"], row["company"], row["description"],
                                 [{"id": f.id, "body": f.body}
                                  for f in selected_facts(facts, sel)])
        sha = ai.payload_sha256({"system": prompt.SYSTEM, "user": user})
        try:
            data, usage = ai.call_json(
                llm, model=ai.SONNET, system=prompt.SYSTEM, user=user,
                schema=prompt.SCHEMA, fx=fx, max_tokens=1600)
        except Exception as exc:
            async with tx() as conn:
                await ai.log_call(
                    conn, purpose="cover_letter",
                    usage=ai.Usage(ai.SONNET, 0, 0, 0.0), sha=sha,
                    entity_type="application", entity_id=row["app_id"],
                    ok=False, error=f"{type(exc).__name__}: {exc}")
            counts["failed"] += 1
            if ai.is_permanent_error(exc):
                counts["stopped"] = f"aborted batch: {type(exc).__name__}"
                break
            continue

        blocks = [b for b in (data.get("paragraphs") or []) if b.get("text")]
        state, findings = provenance.validate(
            blocks, facts=all_facts, job_text=ad,
            extra_allowed=[row["company"] or "", row["title"]],
            parent_bodies=parent_bodies(facts))
        # Advisories do not change the verdict; they are what the approval step
        # should read first.
        findings = findings + provenance.merged_source_advisories(
            blocks, parent_of(facts))

        async with tx() as conn:
            await ai.log_call(conn, purpose="cover_letter", usage=usage, sha=sha,
                              entity_type="application", entity_id=row["app_id"], ok=True)
            await conn.execute(UPSERT_DOCUMENT, {
                "a": row["app_id"], "kind": "COVER_LETTER",
                "pv": row["profile_version_id"],
                "content": json.dumps({"paragraphs": blocks}),
                "sids": provenance.cited_ids(blocks),
                "state": state,
                "findings": json.dumps([{"kind": f.kind, "detail": f.detail}
                                        for f in findings]),
                "model": usage.model})
            await conn.execute(INSERT_EVENT, {
                "a": row["app_id"],
                "action": "cover_letter_drafted" if state == provenance.PASSED
                          else "cover_letter_quarantined",
                "d": json.dumps({"state": state,
                                 "findings": [str(f) for f in findings][:20]})})
            if state != provenance.PASSED:
                await conn.execute(QUARANTINE_NOTIFICATION, {
                    "key": f"fabrication:{row['app_id']}",
                    "p": json.dumps({
                        "application_id": row["app_id"],
                        "job_group_id": row["job_group_id"],
                        "title": row["title"], "company": row["company"],
                        "reason": "cover letter failed provenance validation",
                        "findings": [str(f) for f in findings][:10]})})

        if state == provenance.PASSED:
            counts["letters"] += 1
        else:
            counts["quarantined"] += 1
        counts["cost_aud"] = round(counts["cost_aud"] + usage.cost_aud, 6)

        remaining -= usage.cost_aud
        if remaining <= 0:
            counts["stopped"] = "monthly budget reached mid-batch"
            break

    # Rendering is the renderer's job and runs even for a quarantined letter's
    # resume: a resume is still worth having while the letter is under review.
    async with tx() as conn:
        for row in rows:
            await enqueue(conn, "render", {"application_id": row["app_id"]},
                          dedupe_key=f"render:{row['app_id']}")
            # Separate item, not an inline call: answering is a second paid
            # call, and a failure there must not lose the documents this run
            # already produced.
            await enqueue(conn, "answer", {"application_id": row["app_id"]},
                          dedupe_key=f"answer:{row['app_id']}")
    return counts
