"""Draft answers to application questions, or decline and say so.

Architecture 9, third anti-fabrication mechanism: the answerer emits
`can_answer` first. False writes the question to `open_questions` and surfaces
it, and the application cannot be submitted while a required question is
unanswered. This is the machinery behind "if information is missing, stop and
ask me" -- so a `false` here is the system working, not failing.

An answer that claims can_answer: true still has to survive the provenance
validator. Declining is cheap; a confident wrong answer goes to an employer
with your name on it.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import text

from job_agent.ai import client as ai
from job_agent.ai.prompts import answers as prompt
from job_agent.core.config import get_settings
from job_agent.core.db import tx
from job_agent.generation import provenance
from job_agent.generation.documents import (
    ONE_APPLICATION, _analysis, job_text, load_facts, parent_bodies)
from job_agent.generation.select import facts_by_id, select, selected_facts

QUESTIONS_FILE = Path("/app/config/questions.yaml")

UPSERT_ANSWER = text("""
INSERT INTO app.application_answers
  (application_id, question_key, question_text, answer_text, can_answer,
   required, source_ids)
VALUES (:a, :k, :qt, :at, :can, :req, :sids)
ON CONFLICT (application_id, question_key) DO UPDATE SET
  question_text = EXCLUDED.question_text,
  answer_text = EXCLUDED.answer_text,
  can_answer = EXCLUDED.can_answer,
  required = EXCLUDED.required,
  source_ids = EXCLUDED.source_ids
""")

# Unresolved only: re-running must not pile up duplicates of a question you
# have already answered by hand.
INSERT_OPEN_QUESTION = text("""
INSERT INTO app.open_questions (application_id, question_text, reason)
SELECT :a, :qt, :reason
 WHERE NOT EXISTS (SELECT 1 FROM app.open_questions o
                    WHERE o.application_id = :a AND o.question_text = :qt
                      AND o.resolved_at IS NULL)
""")

INSERT_EVENT = text("""
INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('application', :a, 'generate', 'answers_drafted', CAST(:d AS jsonb))
""")


def load_questions(path: Path | None = None) -> list[dict]:
    """Questions from config. Phases 14-15 pass real form questions instead."""
    p = path or QUESTIONS_FILE
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text()) or []
    return [q for q in data if q.get("question_key") and q.get("question_text")]


def reconcile(questions, replies) -> list[dict]:
    """Pair each asked question with its reply.

    A question the model simply omitted is treated as unanswerable rather than
    dropped: silence is not consent, and the alternative is a required question
    quietly disappearing from the blocking set.
    """
    by_key = {str(r.get("question_key")): r for r in replies}
    out = []
    for q in questions:
        r = by_key.get(q["question_key"]) or {}
        can = bool(r.get("can_answer")) and bool(str(r.get("answer_text") or "").strip())
        out.append({
            "question_key": q["question_key"],
            "question_text": q["question_text"],
            "required": bool(q.get("required", True)),
            "can_answer": can,
            "answer_text": str(r.get("answer_text") or "").strip() if can else None,
            "source_ids": [str(s) for s in (r.get("source_ids") or [])] if can else [],
            "missing_reply": q["question_key"] not in by_key,
        })
    return out


async def answer_questions(application_id: int, questions=None) -> dict:
    counts = {"answered": 0, "open": 0, "quarantined": 0, "cost_aud": 0.0,
              "stopped": None}
    questions = questions if questions is not None else load_questions()
    if not questions:
        counts["stopped"] = "no questions configured"
        return counts

    async with tx() as conn:
        cfg = await ai.load_config(conn)
        fx = float(cfg.get("ai.fx_aud_per_usd", ai.DEFAULT_FX_AUD_PER_USD))
        row = (await conn.execute(
            ONE_APPLICATION, {"a": application_id})).mappings().first()
        if row is None:
            counts["stopped"] = f"no application {application_id}"
            return counts
        facts = await load_facts(conn)
        skills, requirements = await _analysis(conn, row["job_group_id"])
        try:
            await ai.check_budget(conn, cfg)
        except (ai.AIDisabled, ai.BudgetExceeded) as stop:
            counts["stopped"] = str(stop)
            return counts

    sel = select(facts, job_skills=skills, job_requirements=requirements,
                 job_title=row["title"])
    user = prompt.build_user(row["title"], row["company"], row["description"],
                             [{"id": f.id, "body": f.body}
                              for f in selected_facts(facts, sel)],
                             questions)
    sha = ai.payload_sha256({"system": prompt.SYSTEM, "user": user})
    llm = ai.build_client(get_settings().anthropic_api_key)
    try:
        data, usage = ai.call_json(
            llm, model=ai.SONNET, system=prompt.SYSTEM, user=user,
            schema=prompt.SCHEMA, fx=fx, max_tokens=2500)
    except Exception as exc:
        async with tx() as conn:
            await ai.log_call(conn, purpose="answers",
                              usage=ai.Usage(ai.SONNET, 0, 0, 0.0), sha=sha,
                              entity_type="application", entity_id=application_id,
                              ok=False, error=f"{type(exc).__name__}: {exc}")
        counts["stopped"] = f"{type(exc).__name__}: {exc}"
        return counts

    all_facts = facts_by_id(facts)
    ad = job_text(row["title"], row["company"], row["description"])
    paired = reconcile(questions, data.get("answers") or [])

    async with tx() as conn:
        await ai.log_call(conn, purpose="answers", usage=usage, sha=sha,
                          entity_type="application", entity_id=application_id, ok=True)
        for a in paired:
            reason = None
            if a["can_answer"]:
                state, findings = provenance.validate(
                    [{"text": a["answer_text"], "source_ids": a["source_ids"]}],
                    facts=all_facts, job_text=ad,
                    extra_allowed=[row["company"] or "", row["title"]],
                    parent_bodies=parent_bodies(facts))
                if state != provenance.PASSED:
                    # Downgraded, not stored as an answer: an answer that fails
                    # provenance is worse than no answer, because it looks ready.
                    a["can_answer"] = False
                    a["answer_text"] = None
                    a["source_ids"] = []
                    reason = "; ".join(str(f) for f in findings)[:400]
                    counts["quarantined"] += 1
            elif a["missing_reply"]:
                reason = "the model returned no reply for this question"
            else:
                reason = "not answerable from the profile facts supplied"

            await conn.execute(UPSERT_ANSWER, {
                "a": application_id, "k": a["question_key"],
                "qt": a["question_text"], "at": a["answer_text"],
                "can": a["can_answer"], "req": a["required"],
                "sids": a["source_ids"]})
            if a["can_answer"]:
                counts["answered"] += 1
            else:
                await conn.execute(INSERT_OPEN_QUESTION, {
                    "a": application_id, "qt": a["question_text"], "reason": reason})
                counts["open"] += 1

        await conn.execute(INSERT_EVENT, {
            "a": application_id,
            "d": json.dumps({k: counts[k] for k in
                             ("answered", "open", "quarantined")})})

    counts["cost_aud"] = round(usage.cost_aud, 6)
    return counts
