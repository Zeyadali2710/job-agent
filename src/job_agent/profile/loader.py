"""Load YAML profile → validated models → Postgres immutable snapshot."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import yaml

from job_agent.profile.models import Profile, PublicProfile, Sensitive

PROFILE_DIR = Path("/app/profile")


def _load(name: str, default=None):
    path = PROFILE_DIR / name
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"missing profile file: {path}")
    return yaml.safe_load(path.read_text()) or default


def load_profile(directory: Path | None = None) -> Profile:
    global PROFILE_DIR
    if directory:
        PROFILE_DIR = directory
    return Profile(
        identity=_load("identity.yaml"),
        work_rights=_load("work_rights.yaml"),
        preferences=_load("preferences.yaml"),
        education=_load("education.yaml", []),
        experience=_load("experience.yaml", []),
        projects=_load("projects.yaml", []),
        skills=_load("skills.yaml", []),
        star=_load("answers/star.yaml", []),
    )


def load_sensitive(directory: Path | None = None) -> Sensitive:
    d = directory or PROFILE_DIR
    path = d / "sensitive.yaml"
    if not path.exists():
        return Sensitive()
    return Sensitive(**(yaml.safe_load(path.read_text()) or {}))


def iter_facts(p: Profile):
    """Flatten to (fact_id, kind, body, tags, data) rows."""
    for e in p.education:
        yield e.id, "education", f"{e.qualification}, {e.institution}", [], e.model_dump(mode="json")
        for h in e.highlights:
            yield h.id, "education_bullet", h.text, h.tags, h.metrics
    for x in p.experience:
        yield x.id, "experience", f"{x.title}, {x.organisation}", [], x.model_dump(mode="json")
        for b in x.bullets:
            yield b.id, "experience_bullet", b.text, b.tags, b.metrics
    for pr in p.projects:
        yield pr.id, "project", pr.name, [], pr.model_dump(mode="json")
        for b in pr.bullets:
            yield b.id, "project_bullet", b.text, b.tags, b.metrics
    for s in p.skills:
        yield s.id, "skill", s.name, [s.level], {"years": s.years, "evidence_ids": s.evidence_ids}
    for a in p.star:
        yield a.id, "star", a.situation, a.prompt_themes, a.model_dump(mode="json")
    yield p.work_rights.id, "work_rights", p.work_rights.status, [], p.work_rights.model_dump(mode="json")
    yield p.preferences.id, "preferences", "preferences", [], p.preferences.model_dump(mode="json")
    yield p.identity.id, "identity", p.identity.full_name, [], p.identity.model_dump(mode="json")


def validate(p: Profile) -> list[str]:
    """Return a list of problems. Empty list means valid."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for fid, kind, *_ in iter_facts(p):
        if fid in seen:
            problems.append(f"duplicate id {fid!r} (as {seen[fid]} and {kind})")
        seen[fid] = kind

    for s in p.skills:
        for ev in s.evidence_ids:
            if ev not in seen:
                problems.append(f"skill {s.id} cites unknown evidence id {ev!r}")
    for a in p.star:
        for src in a.source_ids:
            if src not in seen:
                problems.append(f"star {a.id} cites unknown source id {src!r}")

    if not p.experience and not p.projects:
        problems.append("profile has neither experience nor projects; nothing to cite")

    leaked = set(PublicProfile.model_fields) & {
        "address", "dob", "phone", "tfn", "visa_grant_number",
        "passport", "referee_contacts", "salary_expectations_floor",
    }
    if leaked:
        problems.append(f"FATAL: PublicProfile exposes sensitive fields: {sorted(leaked)}")
    return problems


def content_hash(p: Profile, s: Sensitive) -> str:
    blob = json.dumps(
        {"profile": p.model_dump(mode="json"), "sensitive": s.model_dump(mode="json")},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def main() -> int:
    p = load_profile()
    s = load_sensitive()
    problems = validate(p)
    if problems:
        for msg in problems:
            print(f"INVALID: {msg}", file=sys.stderr)
        return 1
    facts = list(iter_facts(p))
    print(f"valid: {len(facts)} facts, hash {content_hash(p, s)[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
