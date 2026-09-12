"""Turn drafted content into PDFs. Runs only in the renderer container.

This is the one place in the system that reads `sensitive.profile_sensitive`,
and the container it runs in is attached to the internal network only -- it
cannot reach the API, Discord, or anything else outside the compose stack.
That combination is the point: contact details are template-filled into a
document by a process that has no way to send them anywhere.

Architecture 9: "Contact details are template-filled locally into the PDF after
generation." The model never saw them, because PublicProfile has no field for
them; the renderer adds them afterwards.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from job_agent.ai.client import decode_jsonb
from job_agent.core.db import tx
from job_agent.generation.pdf import Contact, cover_letter_pdf, resume_pdf

DEFAULT_ARTIFACT_DIR = "/app/artifacts"

PENDING = text("""
SELECT d.id, d.application_id, d.kind, d.content, d.validator_state,
       g.title, c.name AS company
  FROM app.generated_documents d
  JOIN app.applications a ON a.id = d.application_id
  JOIN app.job_groups g ON g.id = a.job_group_id
  LEFT JOIN app.companies c ON c.id = g.company_id
 WHERE d.path IS NULL
   AND d.validator_state = 'PASSED'
   AND (:app_id IS NULL OR d.application_id = :app_id)
 ORDER BY d.id
 LIMIT :n
""")

IDENTITY = text("""
SELECT f.data FROM app.profile_facts f
  JOIN app.profile_versions v ON v.id = f.profile_version_id
 WHERE v.is_active AND f.kind = 'identity'
 LIMIT 1
""")

# The only read of the sensitive schema anywhere in the codebase.
SENSITIVE = text("""
SELECT s.field, s.value FROM sensitive.profile_sensitive s
  JOIN app.profile_versions v ON v.id = s.profile_version_id
 WHERE v.is_active AND s.field = ANY(:fields)
""")

SETTING = text("SELECT value FROM app.settings WHERE key = :k")

RECORD = text("""
UPDATE app.generated_documents SET path = :p, sha256 = :h WHERE id = :i
""")

INSERT_EVENT = text("""
INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('application', :a, 'renderer', 'document_rendered', CAST(:d AS jsonb))
""")


async def artifact_dir(conn) -> Path:
    raw = decode_jsonb((await conn.execute(
        SETTING, {"k": "generate.artifact_dir"})).scalar())
    return Path(str(raw or os.environ.get("ARTIFACT_DIR") or DEFAULT_ARTIFACT_DIR))


async def load_contact(conn) -> Contact:
    """Identity from the profile, phone and address from the sensitive schema.

    phone_display on identity is the fallback, not the preference: it is a
    display string the profile author typed, while sensitive.phone is the
    number itself.
    """
    ident = decode_jsonb((await conn.execute(IDENTITY)).scalar()) or {}
    secret = {r.field: r.value for r in (await conn.execute(
        SENSITIVE, {"fields": ["phone", "address"]})).all()}

    city = ident.get("location_city", "")
    state = ident.get("location_state", "")
    location = ", ".join(b for b in (city, state) if b)
    return Contact(
        full_name=ident.get("full_name") or ident.get("preferred_name") or "",
        email=ident.get("email") or "",
        phone=secret.get("phone") or ident.get("phone_display") or "",
        location=secret.get("address") or location,
        links=tuple((l.get("label", ""), l.get("url", ""))
                    for l in (ident.get("links") or []) if l.get("url")),
    )


def _filename(kind: str, application_id: int, sha: str) -> str:
    slug = {"RESUME": "resume", "COVER_LETTER": "cover-letter"}.get(kind, kind.lower())
    return f"app-{application_id:06d}-{slug}-{sha[:12]}.pdf"


def render_document(kind: str, content: dict, contact: Contact, *,
                    title: str = "", company: str | None = None,
                    today: str = "") -> bytes:
    """Pure: content in, PDF bytes out. Kept separate from the DB work so the
    layout can be exercised without a database."""
    if kind == "RESUME":
        return resume_pdf(contact,
                          education=content.get("education") or (),
                          experience=content.get("experience") or (),
                          projects=content.get("projects") or (),
                          skills=content.get("skills") or ())
    if kind == "COVER_LETTER":
        paragraphs = [p.get("text", "") for p in (content.get("paragraphs") or ())]
        return cover_letter_pdf(contact, company=company or "", job_title=title,
                                paragraphs=paragraphs, date=today)
    raise ValueError(f"no renderer for document kind {kind!r}")


async def render_pending(limit: int = 10, application_id: int | None = None) -> dict:
    """Render every PASSED document that has no file yet.

    Quarantined documents are deliberately not rendered: a cover letter that
    failed provenance must not become a PDF sitting next to a good one, where
    the only thing distinguishing them is a database column.
    """
    counts = {"rendered": 0, "failed": 0}
    async with tx() as conn:
        rows = (await conn.execute(
            PENDING, {"n": limit, "app_id": application_id})).mappings().all()
        if not rows:
            return counts
        contact = await load_contact(conn)
        out_dir = await artifact_dir(conn)

    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%d %B %Y")

    for row in rows:
        content = decode_jsonb(row["content"]) or {}
        try:
            blob = render_document(row["kind"], content, contact,
                                   title=row["title"], company=row["company"],
                                   today=today)
        except Exception:
            counts["failed"] += 1
            raise
        sha = hashlib.sha256(blob).hexdigest()
        path = out_dir / _filename(row["kind"], row["application_id"], sha)
        path.write_bytes(blob)

        async with tx() as conn:
            await conn.execute(RECORD, {"p": str(path), "h": sha, "i": row["id"]})
            await conn.execute(INSERT_EVENT, {
                "a": row["application_id"],
                "d": json.dumps({"kind": row["kind"], "bytes": len(blob),
                                 "sha256": sha, "path": str(path)})})
        counts["rendered"] += 1
    return counts
