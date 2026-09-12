"""Re-run the provenance validator over documents that are already drafted.

The validator will keep changing -- it has twice already, once to close a real
hole and once to stop rejecting an employer's own name in the possessive. Both
times the corpus of drafted documents was left carrying a verdict from the
previous version.

Regenerating to re-validate would be paying a model to produce new prose in
order to re-run a pure function, and it would also throw away the drafts you
may already have read. generated_documents.content holds what the validator
checked, so this re-checks it in place. No model is called and nothing is
spent, which also means it works with ai.mode off.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.ai.client import decode_jsonb
from job_agent.core.db import tx
from job_agent.core.queue import enqueue
from job_agent.generation import provenance
from job_agent.generation.documents import (
    job_text, load_facts, parent_bodies, parent_of)
from job_agent.generation.select import facts_by_id

DOCUMENTS = text("""
SELECT d.id, d.application_id, d.kind, d.content, d.validator_state,
       g.title, c.name AS company, p.description
  FROM app.generated_documents d
  JOIN app.applications a ON a.id = d.application_id
  JOIN app.job_groups g ON g.id = a.job_group_id
  JOIN app.postings p ON p.id = g.canonical_posting_id
  LEFT JOIN app.companies c ON c.id = g.company_id
 WHERE d.kind = 'COVER_LETTER'
   AND (CAST(:app_id AS bigint) IS NULL
        OR d.application_id = CAST(:app_id AS bigint))
 ORDER BY d.id
 LIMIT :n
""")

UPDATE_VERDICT = text("""
UPDATE app.generated_documents
   SET validator_state = :state, validator_findings = CAST(:findings AS jsonb),
       source_ids = :sids,
       path = CASE WHEN :state = 'PASSED' THEN path ELSE NULL END,
       sha256 = CASE WHEN :state = 'PASSED' THEN sha256 ELSE NULL END
 WHERE id = :i
""")

EVENT = text("""
INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('application', :a, 'revalidate', 'document_revalidated', CAST(:d AS jsonb))
""")


async def revalidate_pending(limit: int = 50,
                             application_id: int | None = None) -> dict:
    counts = {"checked": 0, "now_passing": 0, "now_failing": 0, "unchanged": 0}

    async with tx() as conn:
        rows = (await conn.execute(
            DOCUMENTS, {"n": limit, "app_id": application_id})).mappings().all()
        facts = await load_facts(conn)

    if not rows or not facts:
        return counts

    all_facts = facts_by_id(facts)
    parents = parent_of(facts)
    bodies = parent_bodies(facts)

    for row in rows:
        content = decode_jsonb(row["content"]) or {}
        blocks = [b for b in (content.get("paragraphs") or []) if b.get("text")]
        if not blocks:
            continue
        ad = job_text(row["title"], row["company"], row["description"])
        state, findings = provenance.validate(
            blocks, facts=all_facts, job_text=ad,
            extra_allowed=[row["company"] or "", row["title"]],
            parent_bodies=bodies)
        findings = findings + provenance.merged_source_advisories(blocks, parents)

        was = row["validator_state"]
        counts["checked"] += 1
        if state == was:
            counts["unchanged"] += 1
        elif state == provenance.PASSED:
            counts["now_passing"] += 1
        else:
            counts["now_failing"] += 1

        async with tx() as conn:
            await conn.execute(UPDATE_VERDICT, {
                "i": row["id"], "state": state,
                "findings": json.dumps([{"kind": f.kind, "detail": f.detail}
                                        for f in findings]),
                "sids": provenance.cited_ids(blocks)})
            await conn.execute(EVENT, {
                "a": row["application_id"],
                "d": json.dumps({"kind": row["kind"], "was": was, "now": state,
                                 "findings": len(findings)})})
            # A document that has just started passing has no PDF yet.
            if state == provenance.PASSED and was != provenance.PASSED:
                await enqueue(conn, "render",
                              {"application_id": row["application_id"]},
                              dedupe_key=f"render:{row['application_id']}:revalidated")
    return counts
