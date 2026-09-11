# PHASES 4–9 — Profile, Sources, Scheduler, Dedup, Classification, Dashboard

Prerequisites: Phase 3 complete, snapshot `phase3-complete` exists, `make health` returns
`role: ja_api` and `schema_version: 0003_applications`.

All commands run **inside CT 120 as `zeyad`** from `~/job-agent` unless marked **[PVE host]**,
where they run as `root`.

Snapshot before each phase. **[PVE host]**:

```bash
pct snapshot 120 pre-phase4 --description "before profile system"
```

At the end of these six phases you have a working discovery pipeline with a dashboard, no AI
and no browser automation. Architecture §14 is right that this is already worth having.

**No new migrations.** The Phase 3 schema covers all six phases. If you find yourself needing
a new table, stop and tell me — it means something in the design drifted.

---

# PHASE 4 — Master Profile System

## 4.1 Objective

YAML profile files, schema-validated, loaded into Postgres as an immutable versioned snapshot
where every atomic fact carries a stable ID. Plus the `PublicProfile` projection that makes it
structurally impossible to serialise your address into a prompt.

Half of this phase is you writing content. The code is one evening; the content is two.

## 4.2 Dependencies

```bash
cat >> requirements.txt <<'EOF'
pyyaml==6.0.2
EOF
docker compose build api
```

## 4.3 Profile files

```bash
mkdir -p profile/{answers/narratives,resumes}
```

Every fact gets an ID. The ID is the anti-fabrication mechanism: a generated sentence about
deploy times must cite `exp.acme.b1`, and the validator kills it if it doesn't.

**ID convention:** `<kind>.<slug>` for entities, `<kind>.<slug>.<n>` for bullets.
Lowercase, dot-separated, no spaces. IDs are permanent — renaming one orphans every document
that cited it.

### `profile/identity.yaml`

```bash
cat > profile/identity.yaml <<'EOF'
id: identity.self
full_name: Zeyad <surname>
preferred_name: Zeyad
email: you@example.com
phone_display: "+61 4XX XXX XXX"   # display only; the real number is in sensitive.yaml
location_city: Sydney
location_state: NSW
links:
  - id: link.github
    label: GitHub
    url: https://github.com/<you>
  - id: link.linkedin
    label: LinkedIn
    url: https://www.linkedin.com/in/<you>
EOF
```

### `profile/work_rights.yaml`

The system never guesses this. It is the source of truth for the hard screen.

```bash
cat > profile/work_rights.yaml <<'EOF'
id: work_rights.self
status: CITIZEN          # CITIZEN | PR | TEMP_GRADUATE_485 | STUDENT_500 | REQUIRES_SPONSORSHIP
citizenship: AU
clearance_held: null     # null | Baseline | NV1 | NV2 | PV
clearance_eligible: true # AGSVA requires AU citizenship
requires_sponsorship: false
EOF
```

### `profile/education.yaml`

```bash
cat > profile/education.yaml <<'EOF'
- id: edu.unsw
  institution: UNSW Sydney
  qualification: Bachelor of Science (Computer Science)
  start: 2022-02
  end: 2025-11
  wam: 78
  completed: true
  highlights:
    - id: edu.unsw.h1
      text: Coursework in distributed systems, security engineering and machine learning
      tags: [distributed-systems, security, ml]
EOF
```

### `profile/experience.yaml`

```bash
cat > profile/experience.yaml <<'EOF'
- id: exp.acme
  organisation: Acme Pty Ltd
  title: Software Engineering Intern
  location: Sydney, NSW
  start: 2024-11
  end: 2025-02
  bullets:
    - id: exp.acme.b1
      text: Built an internal service that reduced deploy time from 25 to 6 minutes
      tags: [python, ci, docker]
      metrics: {before_minutes: 25, after_minutes: 6}
    - id: exp.acme.b2
      text: Wrote integration tests raising coverage on the billing module to 84%
      tags: [testing, pytest]
      metrics: {coverage_pct: 84}
EOF
```

### `profile/projects.yaml`, `profile/skills.yaml`, `profile/preferences.yaml`

```bash
cat > profile/projects.yaml <<'EOF'
- id: proj.jobagent
  name: Self-hosted job application agent
  url: null
  start: 2026-08
  end: null
  bullets:
    - id: proj.jobagent.b1
      text: Designed a Postgres-backed work queue using FOR UPDATE SKIP LOCKED, replacing Celery and Redis
      tags: [postgres, python, architecture]
EOF

cat > profile/skills.yaml <<'EOF'
- id: skill.python
  name: Python
  level: ADVANCED        # BASIC | WORKING | ADVANCED | EXPERT
  years: 4
  evidence_ids: [exp.acme.b1, proj.jobagent.b1]
- id: skill.postgres
  name: PostgreSQL
  level: WORKING
  years: 2
  evidence_ids: [proj.jobagent.b1]
EOF

cat > profile/preferences.yaml <<'EOF'
id: preferences.self
seniority_targets: [GRADUATE, JUNIOR, INTERN]
location_classes_accepted:
  - SYDNEY_CBD
  - SYDNEY_METRO
  - GREATER_SYDNEY
  - HYBRID_SYDNEY
  - REMOTE_AU
allow_relocation: false
employment_types: [FULL_TIME, GRAD_PROGRAM, INTERNSHIP]
salary_min: 70000
salary_target: 90000
salary_is_mandatory: false
company_blacklist: []
EOF

cat > profile/answers/star.yaml <<'EOF'
- id: star.deploytime
  prompt_themes: [initiative, technical-problem-solving]
  situation: The team's deploy pipeline took 25 minutes, discouraging small releases.
  task: Reduce the feedback loop without changing the deployment target.
  action: Profiled the pipeline, parallelised the test stage and cached dependency layers.
  result: Deploys dropped to 6 minutes; release frequency roughly tripled.
  source_ids: [exp.acme.b1]
EOF
```

### `profile/sensitive.yaml`

**Never committed unencrypted.** Phase 2's `.gitignore` already excludes it.

```bash
cat > profile/sensitive.yaml <<'EOF'
address: 1 Example St, Sydney NSW 2000
dob: 2003-01-01
phone: "+61400000000"
tfn: "000000000"
visa_grant_number: null
passport: null
referee_contacts: |
  Jane Smith, Engineering Manager, Acme — jane@example.com, +61400000001
salary_expectations_floor: 70000
emergency_contact: "Name — +61400000002"
EOF
chmod 600 profile/sensitive.yaml
git check-ignore profile/sensitive.yaml && echo "correctly ignored"
```

**Expect** `correctly ignored`. If git does not ignore it, stop and fix `.gitignore` before the
next commit.

## 4.4 Models

```bash
cat > src/job_agent/profile/models.py <<'PYEOF'
"""Profile schema. Every atomic fact carries a stable ID."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9_-]+)+$")

WorkRightStatus = Literal[
    "CITIZEN", "PR", "TEMP_GRADUATE_485", "STUDENT_500", "REQUIRES_SPONSORSHIP"
]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ided(Base):
    id: str

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"bad id {v!r}: expected lowercase dotted, e.g. exp.acme.b1")
        return v


class Bullet(Ided):
    text: str = Field(min_length=10)
    tags: list[str] = []
    metrics: dict[str, float | int | str] = {}


class Link(Ided):
    label: str
    url: str


class Identity(Ided):
    full_name: str
    preferred_name: str
    email: str
    phone_display: str | None = None
    location_city: str
    location_state: str
    links: list[Link] = []


class WorkRights(Ided):
    status: WorkRightStatus
    citizenship: str
    clearance_held: str | None = None
    clearance_eligible: bool = False
    requires_sponsorship: bool = False


class Education(Ided):
    institution: str
    qualification: str
    start: date | str
    end: date | str | None = None
    wam: float | None = None
    completed: bool = False
    highlights: list[Bullet] = []


class Experience(Ided):
    organisation: str
    title: str
    location: str | None = None
    start: date | str
    end: date | str | None = None
    bullets: list[Bullet] = []


class Project(Ided):
    name: str
    url: str | None = None
    start: date | str | None = None
    end: date | str | None = None
    bullets: list[Bullet] = []


class Skill(Ided):
    name: str
    level: Literal["BASIC", "WORKING", "ADVANCED", "EXPERT"]
    years: float | None = None
    evidence_ids: list[str] = []


class Preferences(Ided):
    seniority_targets: list[str]
    location_classes_accepted: list[str]
    allow_relocation: bool = False
    employment_types: list[str]
    salary_min: int
    salary_target: int
    salary_is_mandatory: bool = False
    company_blacklist: list[str] = []


class StarAnswer(Ided):
    prompt_themes: list[str] = []
    situation: str
    task: str
    action: str
    result: str
    source_ids: list[str] = []


class Sensitive(Base):
    """Loaded into schema `sensitive`. Never referenced by PublicProfile."""
    address: str | None = None
    dob: date | str | None = None
    phone: str | None = None
    tfn: str | None = None
    visa_grant_number: str | None = None
    passport: str | None = None
    referee_contacts: str | None = None
    salary_expectations_floor: int | None = None
    emergency_contact: str | None = None


class Profile(Base):
    identity: Identity
    work_rights: WorkRights
    preferences: Preferences
    education: list[Education] = []
    experience: list[Experience] = []
    projects: list[Project] = []
    skills: list[Skill] = []
    star: list[StarAnswer] = []


class PublicProfile(Base):
    """The ONLY profile type an LLM prompt may be built from.

    Address, dob, phone, tfn, visa numbers, passport, referees and the salary floor
    are not fields on this model, so they cannot be serialised into a prompt.
    This is a type-system guarantee, not a code-review guarantee.
    """
    preferred_name: str
    location_city: str
    location_state: str
    work_right_status: WorkRightStatus
    clearance_held: str | None
    education: list[Education]
    experience: list[Experience]
    projects: list[Project]
    skills: list[Skill]
    star: list[StarAnswer]

    @classmethod
    def project(cls, p: Profile) -> "PublicProfile":
        return cls(
            preferred_name=p.identity.preferred_name,
            location_city=p.identity.location_city,
            location_state=p.identity.location_state,
            work_right_status=p.work_rights.status,
            clearance_held=p.work_rights.clearance_held,
            education=p.education,
            experience=p.experience,
            projects=p.projects,
            skills=p.skills,
            star=p.star,
        )
PYEOF
```

## 4.5 Loader and validator

```bash
cat > src/job_agent/profile/loader.py <<'PYEOF'
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
PYEOF
```

### The database writer

Loading touches schema `sensitive`, which only `ja_renderer` can read and no service role can
write. The loader is a human-initiated administrative action, so it runs as the bootstrap
superuser via a `tools`-profile service — the same pattern as `migrator`. No new role.

```bash
cat > src/job_agent/profile/store.py <<'PYEOF'
"""Persist a validated profile as an immutable version. Run as the bootstrap superuser."""
from __future__ import annotations

import json
import os
import sys

import psycopg

from job_agent.profile.loader import (
    content_hash, iter_facts, load_profile, load_sensitive, validate,
)


def dsn() -> str:
    pw = open("/run/secrets/postgres_password").read().strip()
    user = os.environ["POSTGRES_USER"]
    db = os.environ["POSTGRES_DB"]
    host = os.environ.get("POSTGRES_HOST", "postgres")
    return f"host={host} user={user} password={pw} dbname={db}"


def main() -> int:
    p = load_profile()
    s = load_sensitive()
    problems = validate(p)
    if problems:
        for msg in problems:
            print(f"INVALID: {msg}", file=sys.stderr)
        return 1

    h = content_hash(p, s)
    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM app.profile_versions WHERE content_sha256 = %s", (h,))
        row = cur.fetchone()
        if row:
            print(f"unchanged: version {row[0]} already holds hash {h[:12]}")
            cur.execute("UPDATE app.profile_versions SET is_active = (id = %s)", (row[0],))
            conn.commit()
            return 0

        cur.execute(
            "INSERT INTO app.profile_versions (content_sha256, snapshot, is_active) "
            "VALUES (%s, %s, false) RETURNING id",
            (h, json.dumps(p.model_dump(mode="json"))),
        )
        vid = cur.fetchone()[0]

        for fid, kind, body, tags, data in iter_facts(p):
            cur.execute(
                "INSERT INTO app.profile_facts "
                "(profile_version_id, fact_id, kind, body, tags, data) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (vid, fid, kind, str(body), list(tags), json.dumps(data, default=str)),
            )

        for field, value in s.model_dump(mode="json").items():
            if value is None:
                continue
            cur.execute(
                "INSERT INTO sensitive.profile_sensitive (profile_version_id, field, value) "
                "VALUES (%s,%s,%s)",
                (vid, field, str(value)),
            )

        cur.execute("UPDATE app.profile_versions SET is_active = (id = %s)", (vid,))
        cur.execute(
            "INSERT INTO app.events (entity_type, entity_id, actor, action, detail) "
            "VALUES ('profile_version', %s, 'phase-4', 'profile_loaded', %s)",
            (vid, json.dumps({"hash": h})),
        )
        conn.commit()
        print(f"loaded: version {vid}, hash {h[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PYEOF
```

### Compose service

```yaml
  profile-loader:
    build: .
    profiles: ["tools"]
    restart: "no"
    mem_limit: 256m
    memswap_limit: 256m
    cpus: 1.0
    read_only: true
    tmpfs: [/tmp:size=32m]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_HOST: postgres
      TZ: UTC
    secrets: [postgres_password]
    volumes:
      - ./profile:/app/profile:ro
    depends_on:
      postgres: {condition: service_healthy}
    command: ["python", "-m", "job_agent.profile.store"]
    networks: [internal]
```

```bash
cat >> Makefile <<'EOF'
.PHONY: profile-check profile-load
profile-check: ; docker compose run --rm profile-loader python -m job_agent.profile.loader
profile-load:  ; docker compose run --rm profile-loader
EOF
```

## 4.6 Verification

**P4-T1 — Validation catches a broken reference**

```bash
docker compose build api
make profile-check
```

**Expect** `valid: N facts, hash xxxxxxxxxxxx`. Now break it deliberately:

```bash
sed -i 's/exp.acme.b1, proj.jobagent.b1/exp.acme.b1, proj.doesnotexist/' profile/skills.yaml
make profile-check; echo "exit=$?"
sed -i 's/proj.doesnotexist/proj.jobagent.b1/' profile/skills.yaml
```

**Right:** `INVALID: skill skill.python cites unknown evidence id 'proj.doesnotexist'` and
`exit=1`. **Wrong:** exit 0 — the validator isn't running, and the anti-fabrication chain has
no foundation.

**P4-T2 — Load**

```bash
make profile-load
make profile-load    # second run must be a no-op
```

**Right:** `loaded: version 1, hash ...` then `unchanged: version 1 already holds hash ...`.
**Wrong:** a second version row on identical input — the hash is not stable, usually because
something non-deterministic (a set, a timestamp) is in `content_hash`.

**P4-T3 — Facts landed, sensitive is isolated**

```bash
DB=$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$(grep '^POSTGRES_USER=' .env | cut -d= -f2)
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB" <<'SQL'
SELECT count(*) AS facts FROM app.profile_facts;
SELECT kind, count(*) FROM app.profile_facts GROUP BY 1 ORDER BY 2 DESC;
SELECT field FROM sensitive.profile_sensitive ORDER BY 1;
SELECT count(*) AS active FROM app.profile_versions WHERE is_active;
SQL
```

**Right:** facts > 10, sensitive fields listed, `active = 1`.

**P4-T4 — The projection guarantee**

```bash
docker compose run --rm profile-loader python - <<'PY'
from job_agent.profile.loader import load_profile
from job_agent.profile.models import PublicProfile
pub = PublicProfile.project(load_profile())
blob = pub.model_dump_json()
banned = ["address", "tfn", "dob", "passport", "referee", "visa_grant", "salary_expectations"]
hits = [b for b in banned if b in blob.lower()]
print("PublicProfile fields:", sorted(PublicProfile.model_fields))
print("LEAKED:", hits if hits else "none")
raise SystemExit(1 if hits else 0)
PY
echo "exit=$?"
```

**Right:** `LEAKED: none`, `exit=0`. **Wrong:** any hit — a sensitive field was added to
`PublicProfile`, and Phase 11 would send it to Anthropic.

**P4-T5 — `ja_worker` still cannot read sensitive**

```bash
docker compose exec -e PGPASSWORD=$(cat /opt/job-agent/secrets/ja_worker_password) -T postgres \
  psql -h 127.0.0.1 -U ja_worker -d $DB -tAc 'SELECT count(*) FROM sensitive.profile_sensitive;' 2>&1 | head -1
```

**Right:** `ERROR: permission denied for schema sensitive` — now with your real data behind it.

## 4.7 Common errors

**`ValidationError: extra fields not permitted`**
`extra="forbid"` is deliberate. A typo'd YAML key is a silent data loss otherwise. Fix the key.

**`bad id 'Exp.Acme'`**
IDs are lowercase dotted. The regex rejects capitals and single-segment IDs.

**`FileNotFoundError: /app/profile/identity.yaml`**
The bind mount is `./profile:/app/profile:ro` — run compose from `~/job-agent`.

**`permission denied for schema sensitive` from the loader**
It's connecting as a `ja_*` role instead of the bootstrap superuser. Check
`POSTGRES_USER: ${POSTGRES_USER}` in the `profile-loader` service.

## 4.8 Checklist

- [ ] All eight profile YAML files exist and `profile/sensitive.yaml` is git-ignored
- [ ] `make profile-check` reports valid; a broken reference makes it exit 1 (**P4-T1**)
- [ ] `make profile-load` is idempotent on unchanged input (**P4-T2**)
- [ ] `profile_facts` populated, exactly one active version (**P4-T3**)
- [ ] `PublicProfile` leaks nothing (**P4-T4**)
- [ ] `ja_worker` still denied on `sensitive` (**P4-T5**)
- [ ] **[PVE host]** `pct snapshot 120 phase4-complete`

---

# PHASE 5 — Source Framework and First Adapters

## 5.1 Objective

An adapter ABC, rate limiting, a circuit breaker, HTTP fetching into immutable
`raw_postings`, normalisation into `postings`, and working Greenhouse, Lever and Ashby
adapters. Plus the company registry, which is manual research and the highest-value work in
the project.

## 5.2 Dependencies and the worker container

```bash
cat >> requirements.txt <<'EOF'
httpx==0.28.1
selectolax==0.3.27
EOF
```

`worker` is the first service that needs internet egress, so it joins `edge` as well as
`internal`. That is a deliberate, visible decision per service.

```yaml
  worker:
    build: .
    restart: unless-stopped
    mem_limit: 1024m
    memswap_limit: 1024m
    cpus: 2.0
    pids_limit: 256
    init: true
    read_only: true
    tmpfs: [/tmp:size=128m]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    environment:
      DRY_RUN: ${DRY_RUN}
      LOG_LEVEL: ${LOG_LEVEL}
      TZ: UTC
      POSTGRES_USER: ja_worker
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_HOST: postgres
      WORKER_NAME: worker-1
    secrets: [ja_worker_password]
    volumes:
      - ./config:/app/config:ro
    depends_on:
      postgres: {condition: service_healthy}
    command: ["python", "-m", "job_agent.workers.discovery", "--loop"]
    logging: *default-logging
    networks: [internal, edge]
```

Add `ja_worker_password` to the top-level `secrets:` map.

## 5.3 Database helper

```bash
cat > src/job_agent/core/db.py <<'PYEOF'
"""Shared async engine. pool_pre_ping is not optional — see Phase 3 §0.6."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from job_agent.core.config import get_settings

_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            pool_size=4, max_overflow=4,
            pool_pre_ping=True, pool_recycle=1800, pool_timeout=10,
            connect_args={
                "timeout": 5,
                "server_settings": {
                    "application_name": os.environ.get("WORKER_NAME", "job-agent"),
                    "timezone": "UTC",
                },
            },
        )
    return _engine


@asynccontextmanager
async def tx():
    async with engine().begin() as conn:
        yield conn
PYEOF
```

## 5.4 Adapter ABC, rate limiter, circuit breaker

```bash
cat > src/job_agent/sources/base.py <<'PYEOF'
from __future__ import annotations

import asyncio
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator


@dataclass(slots=True)
class RawPosting:
    source_job_id: str
    url: str | None
    payload: dict[str, Any]

    @property
    def payload_sha256(self) -> str:
        import json
        blob = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(slots=True)
class NormalisedPosting:
    source_job_id: str
    title: str
    description: str | None
    location_raw: str | None
    posted_at: datetime | None
    posted_at_confidence: str
    posted_at_raw: str | None
    canonical_url: str | None
    ats_platform: str | None
    ats_tenant: str | None
    ats_job_id: str | None
    company_name: str | None
    closes_at: datetime | None = None
    employment_type: str | None = None
    salary: dict = field(default_factory=dict)


class TokenBucket:
    """Simple async rate limiter: `rate` requests per second, burst `capacity`."""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate, self.capacity = rate, capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self.rate)
                self._tokens = 0
            else:
                self._tokens -= 1


class CircuitOpen(RuntimeError):
    pass


class SourceAdapter(ABC):
    key: str
    platform: str | None = None
    rate = TokenBucket(rate=2.0, capacity=4)

    @abstractmethod
    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        """Yield immutable raw records for one tenant."""

    @abstractmethod
    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        """Pure function: raw payload → normalised fields. No I/O, no DB."""
PYEOF
```

`normalise` is deliberately pure so Phase 7's dedup tests and Phase 8's classification tests
can run against captured fixtures with no network and no database.

## 5.5 URL canonicalisation

```bash
cat > src/job_agent/sources/urls.py <<'PYEOF'
from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STRIP_PREFIXES = ("utm_", "gh_", "mc_", "_hs")
STRIP_EXACT = {"ref", "source", "src", "session", "sessionid", "trk", "trackingId", "fbclid", "gclid"}


def canonicalise(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = parts.hostname.lower() if parts.hostname else ""
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(STRIP_PREFIXES) and k not in STRIP_EXACT
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def url_hash(url: str | None, fallback: str = "") -> str:
    return hashlib.sha256((canonicalise(url) or fallback).encode()).hexdigest()


def title_norm(title: str) -> str:
    import re
    t = title.lower()
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)              # drop bracketed suffixes
    t = re.sub(r"[^a-z0-9+#/ ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
PYEOF
```

`url_hash` takes a fallback because `postings.url_hash` is `NOT NULL UNIQUE` — a source with
no URL must still produce a distinct hash. Pass `f"{source_key}:{source_job_id}"`.

## 5.6 Adapters

```bash
cat > src/job_agent/sources/ats/greenhouse.py <<'PYEOF'
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx
from selectolax.parser import HTMLParser

from job_agent.sources.base import NormalisedPosting, RawPosting, SourceAdapter, TokenBucket
from job_agent.sources.urls import canonicalise

API = "https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true"


class GreenhouseAdapter(SourceAdapter):
    key = "greenhouse"
    platform = "greenhouse"
    rate = TokenBucket(rate=2.0, capacity=4)

    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        await self.rate.take()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(API.format(tenant=tenant),
                            headers={"User-Agent": "job-agent/1.0 (personal job search)"})
            r.raise_for_status()
            for job in r.json().get("jobs", []):
                yield RawPosting(
                    source_job_id=str(job["id"]),
                    url=job.get("absolute_url"),
                    payload=job,
                )

    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        j = raw.payload
        posted_raw = j.get("first_published") or j.get("updated_at")
        posted_at, conf = None, "UNKNOWN"
        if posted_raw:
            posted_at = datetime.fromisoformat(posted_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            conf = "EXACT" if j.get("first_published") else "INFERRED"
        content = j.get("content") or ""
        text = HTMLParser(content.replace("&lt;", "<").replace("&gt;", ">")).text(separator="\n")
        return NormalisedPosting(
            source_job_id=str(j["id"]),
            title=j.get("title", "").strip(),
            description=text.strip() or None,
            location_raw=(j.get("location") or {}).get("name"),
            posted_at=posted_at,
            posted_at_confidence=conf,
            posted_at_raw=posted_raw,
            canonical_url=canonicalise(j.get("absolute_url")),
            ats_platform="greenhouse",
            ats_tenant=tenant,
            ats_job_id=str(j["id"]),
            company_name=(j.get("company_name") or tenant),
        )
PYEOF

cat > src/job_agent/sources/ats/lever.py <<'PYEOF'
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from job_agent.sources.base import NormalisedPosting, RawPosting, SourceAdapter, TokenBucket
from job_agent.sources.urls import canonicalise

API = "https://api.lever.co/v0/postings/{tenant}?mode=json"


class LeverAdapter(SourceAdapter):
    key = "lever"
    platform = "lever"
    rate = TokenBucket(rate=1.0, capacity=3)

    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        await self.rate.take()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(API.format(tenant=tenant),
                            headers={"User-Agent": "job-agent/1.0 (personal job search)"})
            r.raise_for_status()
            for job in r.json():
                yield RawPosting(source_job_id=job["id"], url=job.get("hostedUrl"), payload=job)

    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        j = raw.payload
        created = j.get("createdAt")
        posted_at = (
            datetime.fromtimestamp(created / 1000, tz=timezone.utc) if created else None
        )
        cats = j.get("categories") or {}
        return NormalisedPosting(
            source_job_id=j["id"],
            title=(j.get("text") or "").strip(),
            description=j.get("descriptionPlain") or j.get("description"),
            location_raw=cats.get("location"),
            posted_at=posted_at,
            posted_at_confidence="EXACT" if created else "UNKNOWN",
            posted_at_raw=str(created) if created else None,
            canonical_url=canonicalise(j.get("hostedUrl")),
            ats_platform="lever",
            ats_tenant=tenant,
            ats_job_id=j["id"],
            company_name=tenant,
            employment_type=_commitment(cats.get("commitment")),
        )


def _commitment(v: str | None) -> str | None:
    if not v:
        return None
    v = v.lower()
    if "intern" in v:
        return "INTERNSHIP"
    if "full" in v:
        return "FULL_TIME"
    if "part" in v:
        return "PART_TIME"
    if "contract" in v or "temp" in v:
        return "CONTRACT"
    return None
PYEOF

cat > src/job_agent/sources/ats/ashby.py <<'PYEOF'
from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from job_agent.sources.base import NormalisedPosting, RawPosting, SourceAdapter, TokenBucket
from job_agent.sources.urls import canonicalise

API = "https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true"


class AshbyAdapter(SourceAdapter):
    key = "ashby"
    platform = "ashby"
    rate = TokenBucket(rate=1.0, capacity=3)

    async def fetch(self, tenant: str) -> AsyncIterator[RawPosting]:
        await self.rate.take()
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(API.format(tenant=tenant),
                            headers={"User-Agent": "job-agent/1.0 (personal job search)"})
            r.raise_for_status()
            for job in r.json().get("jobs", []):
                if not job.get("isListed", True):
                    continue                      # unlisted: not publicly advertised
                yield RawPosting(source_job_id=job["id"], url=job.get("jobUrl"), payload=job)

    def normalise(self, raw: RawPosting, tenant: str) -> NormalisedPosting:
        j = raw.payload
        pub = j.get("publishedAt")
        posted_at = (
            datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc)
            if pub else None
        )
        comp = j.get("compensation") or {}
        return NormalisedPosting(
            source_job_id=j["id"],
            title=(j.get("title") or "").strip(),
            description=j.get("descriptionPlain"),
            location_raw=j.get("location"),
            posted_at=posted_at,
            posted_at_confidence="EXACT" if pub else "UNKNOWN",
            posted_at_raw=pub,
            canonical_url=canonicalise(j.get("jobUrl")),
            ats_platform="ashby",
            ats_tenant=tenant,
            ats_job_id=j["id"],
            company_name=j.get("organizationName") or tenant,
            salary={"raw": comp} if comp else {},
        )
PYEOF

cat > src/job_agent/sources/registry.py <<'PYEOF'
from job_agent.sources.ats.ashby import AshbyAdapter
from job_agent.sources.ats.greenhouse import GreenhouseAdapter
from job_agent.sources.ats.lever import LeverAdapter

ADAPTERS = {a.key: a() for a in (GreenhouseAdapter, LeverAdapter, AshbyAdapter)}
PYEOF
```

## 5.7 The company registry

This is the manual research, and it is what makes the whole system work. Find a company's ATS
by loading its careers page and watching the network tab, or from the URL shape:
`boards.greenhouse.io/X` → greenhouse tenant `X`; `jobs.lever.co/X` → lever `X`;
`jobs.ashbyhq.com/X` → ashby `X`.

```bash
cat > config/companies.yaml <<'EOF'
# company → (ats platform, tenant token)
# Verify each tenant by opening the API URL in a browser before adding it.
#   greenhouse: https://boards-api.greenhouse.io/v1/boards/<tenant>/jobs
#   lever:      https://api.lever.co/v0/postings/<tenant>?mode=json
#   ashby:      https://api.ashbyhq.com/posting-api/job-board/<tenant>
companies:
  - name: Canva
    slug: canva
    platform: lever
    tenant: canva
  - name: SafetyCulture
    slug: safetyculture
    platform: lever
    tenant: safetyculture
EOF
```

**Do not paste a tenant you have not opened in a browser.** A guessed token returns 404, the
circuit breaker opens, and you spend an evening debugging an adapter that is fine.

Target 150–300 employers over two evenings: ASX-listed tech and banks, Atlassian, Canva,
SafetyCulture, Airwallex, Immutable, WiseTech, Rokt, Culture Amp, Linktree, Domain, REA,
Xero AU, the consultancies, the cyber firms.

## 5.8 The discovery worker

```bash
cat > src/job_agent/workers/discovery.py <<'PYEOF'
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import yaml
from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.sources.registry import ADAPTERS
from job_agent.sources.urls import title_norm, url_hash

CONFIG = "/app/config/companies.yaml"
WORKER = os.environ.get("WORKER_NAME", "worker-1")

INSERT_RAW = text("""
INSERT INTO app.raw_postings (source_id, source_job_id, url, http_status, payload, payload_sha256)
SELECT s.id, :sjid, :url, 200, CAST(:payload AS jsonb), :sha
  FROM app.sources s WHERE s.key = :skey
ON CONFLICT (source_id, source_job_id, payload_sha256) DO NOTHING
RETURNING id
""")

UPSERT_POSTING = text("""
INSERT INTO app.postings (
  raw_id, source_id, source_job_id, company_id, title, title_norm, description,
  location_raw, posted_at, posted_at_confidence, posted_at_raw, closes_at,
  ats_platform, ats_tenant, ats_job_id, canonical_url, url_hash,
  employment_type, salary, state)
SELECT :raw_id, s.id, :sjid,
       (SELECT id FROM app.companies WHERE slug = :slug),
       :title, :title_norm, :description, :location_raw, :posted_at,
       :conf, :posted_raw, :closes_at, :platform, :tenant, :ats_job_id,
       :curl, :uhash, :etype, CAST(:salary AS jsonb), 'NORMALISED'
  FROM app.sources s WHERE s.key = :skey
ON CONFLICT (source_id, source_job_id) DO UPDATE SET
  last_seen_at = now(),
  title = EXCLUDED.title,
  description = EXCLUDED.description,
  closes_at = EXCLUDED.closes_at,
  updated_at = now()
RETURNING id, (xmax = 0) AS inserted
""")


def load_companies() -> list[dict]:
    return (yaml.safe_load(open(CONFIG).read()) or {}).get("companies", [])


async def circuit_ok(conn, skey: str) -> bool:
    row = (await conn.execute(text(
        "SELECT circuit_state, circuit_opened_at FROM app.sources WHERE key = :k"), {"k": skey})).first()
    if not row:
        return False
    state, opened = row
    if state == "OPEN" and opened is not None:
        await conn.execute(text(
            "UPDATE app.sources SET circuit_state='HALF_OPEN' "
            "WHERE key=:k AND circuit_opened_at < now() - interval '30 minutes'"), {"k": skey})
        row2 = (await conn.execute(text(
            "SELECT circuit_state FROM app.sources WHERE key=:k"), {"k": skey})).scalar()
        return row2 != "OPEN"
    return True


async def record_failure(conn, skey: str, err: str) -> None:
    await conn.execute(text("""
        UPDATE app.sources
           SET consecutive_failures = consecutive_failures + 1,
               circuit_state = CASE WHEN consecutive_failures + 1 >= 3 THEN 'OPEN' ELSE circuit_state END,
               circuit_opened_at = CASE WHEN consecutive_failures + 1 >= 3 THEN now() ELSE circuit_opened_at END
         WHERE key = :k"""), {"k": skey})
    await conn.execute(text(
        "INSERT INTO app.events (entity_type, actor, action, detail) "
        "VALUES ('source', :a, 'fetch_failed', CAST(:d AS jsonb))"),
        {"a": WORKER, "d": json.dumps({"source": skey, "error": err[:500]})})


async def poll_source(skey: str) -> tuple[int, int]:
    adapter = ADAPTERS[skey]
    companies = [c for c in load_companies() if c["platform"] == skey]
    new_raw = new_postings = 0

    for company in companies:
        tenant = company["tenant"]
        try:
            async with tx() as conn:
                if not await circuit_ok(conn, skey):
                    print(f"circuit open for {skey}, skipping", file=sys.stderr)
                    return 0, 0

            records = [r async for r in adapter.fetch(tenant)]
        except Exception as exc:
            async with tx() as conn:
                await record_failure(conn, skey, f"{type(exc).__name__}: {exc}")
            print(f"FAIL {skey}/{tenant}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        async with tx() as conn:
            await conn.execute(text(
                "UPDATE app.sources SET consecutive_failures=0, circuit_state='CLOSED' WHERE key=:k"),
                {"k": skey})
            for raw in records:
                rid = (await conn.execute(INSERT_RAW, {
                    "skey": skey, "sjid": raw.source_job_id, "url": raw.url,
                    "payload": json.dumps(raw.payload, default=str),
                    "sha": raw.payload_sha256,
                })).scalar()
                if rid is None:
                    continue                       # byte-identical repeat; nothing changed
                new_raw += 1

                n = adapter.normalise(raw, tenant)
                res = (await conn.execute(UPSERT_POSTING, {
                    "raw_id": rid, "skey": skey, "sjid": n.source_job_id,
                    "slug": company["slug"], "title": n.title,
                    "title_norm": title_norm(n.title), "description": n.description,
                    "location_raw": n.location_raw, "posted_at": n.posted_at,
                    "conf": n.posted_at_confidence, "posted_raw": n.posted_at_raw,
                    "closes_at": n.closes_at, "platform": n.ats_platform,
                    "tenant": n.ats_tenant, "ats_job_id": n.ats_job_id,
                    "curl": n.canonical_url,
                    "uhash": url_hash(n.canonical_url, f"{skey}:{n.source_job_id}"),
                    "etype": n.employment_type, "salary": json.dumps(n.salary),
                })).first()
                if res and res.inserted:
                    new_postings += 1

    async with tx() as conn:
        await conn.execute(text(
            "INSERT INTO app.heartbeats (worker, last_beat, detail) VALUES (:w, now(), CAST(:d AS jsonb)) "
            "ON CONFLICT (worker) DO UPDATE SET last_beat = now(), detail = EXCLUDED.detail"),
            {"w": WORKER, "d": json.dumps({"last_source": skey, "new": new_postings})})
    return new_raw, new_postings


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="one adapter key; omit for all")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    keys = [args.source] if args.source else list(ADAPTERS)
    while True:
        for k in keys:
            raw, posts = await poll_source(k)
            print(f"{k}: {raw} raw, {posts} new postings")
        if not args.loop:
            return 0
        await asyncio.sleep(1800)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
PYEOF
```

`xmax = 0` distinguishes an insert from an update in an upsert — the only reliable way to
count genuinely new postings without a second query.

## 5.9 Verification

```bash
docker compose build worker
```

**P5-T1 — Canonicalisation and hashing**

```bash
docker compose run --rm worker python - <<'PY'
from job_agent.sources.urls import canonicalise, title_norm, url_hash
a = canonicalise("https://WWW.Example.com/jobs/12/?utm_source=x&gh_src=y&b=2")
b = canonicalise("https://example.com/jobs/12?b=2")
print(a); print(b); print("match:", a == b)
print("hash stable:", url_hash(a) == url_hash(b))
print(title_norm("Graduate Software Engineer (2027 Intake) — Sydney"))
PY
```

**Right:** both URLs identical, `match: True`, `hash stable: True`, title normalised to
`graduate software engineer sydney`. **Wrong:** differing URLs — tracking params leak into
`url_hash` and dedupe ladder layer 3 stops working.

**P5-T2 — One real fetch**

```bash
docker compose run --rm worker python -m job_agent.workers.discovery --source lever --once
```

**Right:** `lever: N raw, N new postings` with N > 0.
**Wrong:** `FAIL lever/<tenant>: HTTPStatusError ... 404` — the tenant token is wrong. Open
`https://api.lever.co/v0/postings/<tenant>?mode=json` in a browser and correct
`config/companies.yaml`.

**P5-T3 — Raw is immutable and deduped**

```bash
docker compose run --rm worker python -m job_agent.workers.discovery --source lever --once
```

**Right:** second run reports `0 raw, 0 new postings` — identical payloads hit the
`ON CONFLICT` and nothing is written. **Wrong:** raw count climbing on every run means
`payload_sha256` is unstable, usually a timestamp inside the payload.

**P5-T4 — Data landed correctly**

```bash
make psql-ro
```
```sql
SELECT count(*) FROM app.raw_postings;
SELECT source_id, count(*), min(posted_at), max(posted_at) FROM app.postings GROUP BY 1;
SELECT posted_at_confidence, count(*) FROM app.postings GROUP BY 1;
SELECT title, location_raw, posted_at AT TIME ZONE 'Australia/Sydney' AS sydney
  FROM app.postings ORDER BY posted_at DESC NULLS LAST LIMIT 5;
SELECT count(*) FROM app.postings WHERE description IS NULL;
```

**Right:** `posted_at` populated with `EXACT` confidence for Lever and Ashby; descriptions
present. **Wrong:** all `posted_at` NULL — the epoch conversion is wrong; all descriptions
NULL — the field name changed in the API.

**P5-T5 — Circuit breaker**

```bash
sed -i 's/tenant: canva/tenant: definitely-not-a-real-tenant/' config/companies.yaml
for i in 1 2 3; do docker compose run --rm worker python -m job_agent.workers.discovery --source lever --once; done
make psql-ro -e 2>/dev/null || true
```
```sql
SELECT key, circuit_state, consecutive_failures FROM app.sources WHERE key='lever';
SELECT action, detail->>'error' FROM app.events WHERE action='fetch_failed' ORDER BY at DESC LIMIT 3;
```

**Right:** `circuit_state = OPEN`, `consecutive_failures >= 3`, failure events logged.
Restore the tenant and re-run; state returns to `CLOSED`.

**P5-T6 — Worker egress is scoped**

```bash
docker compose exec -T worker python -c "import socket;socket.create_connection(('1.1.1.1',53),timeout=3)" \
  && echo "worker has egress (expected)"
docker compose exec -T api python -c "import socket;socket.create_connection(('1.1.1.1',53),timeout=3)" \
  2>&1 | tail -1
```

**Right:** worker succeeds, api still fails. **Wrong:** api succeeding means it was added to
`edge` by accident.

## 5.10 Common errors

**`httpx.ConnectError` for every tenant**
The worker isn't on the `edge` network. Check `docker compose config | grep -A3 'worker:' `.

**`null value in column "url_hash"`**
A posting with no URL and no fallback passed. The fallback argument to `url_hash` is not
optional.

**`duplicate key value violates unique constraint "postings_url_hash"`**
Two sources produced the same canonical URL. That is a real duplicate — Phase 7 collapses it
into one group. Until then, the second insert legitimately fails; note the URL and move on.

**All postings have `company_id` NULL**
The `slug` in `config/companies.yaml` doesn't match a row in `app.companies`. Add the company
with `make seed` or an INSERT.

## 5.11 Checklist

- [ ] `worker` builds and runs; `docker stats` shows it under 1024m
- [ ] URL canonicalisation strips tracking params (**P5-T1**)
- [ ] At least one adapter returns real postings (**P5-T2**)
- [ ] Re-running produces 0 new raw rows (**P5-T3**)
- [ ] `posted_at` and `posted_at_confidence` populated (**P5-T4**)
- [ ] Circuit breaker opens after 3 failures and recovers (**P5-T5**)
- [ ] Worker has egress; api still does not (**P5-T6**)
- [ ] `config/companies.yaml` has ≥ 20 verified tenants
- [ ] **[PVE host]** `pct snapshot 120 phase5-complete`

---

# PHASE 6 — Scheduler and Queue

## 6.1 Objective

APScheduler enqueues work items and returns in milliseconds; a worker loop claims them with
`FOR UPDATE SKIP LOCKED`. Schedule survives restarts. Twelve missed runs produce one catch-up,
not twelve.

## 6.2 Dependencies

```bash
cat >> requirements.txt <<'EOF'
apscheduler==3.11.0
EOF
```

APScheduler 3.x uses a sync SQLAlchemy jobstore, so it needs the `psycopg` driver already
present from Phase 3.

## 6.3 The queue

```bash
cat > src/job_agent/core/queue.py <<'PYEOF'
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

CLAIM = text("""
WITH claimed AS (
  SELECT id FROM app.work_items
   WHERE state = 'PENDING' AND run_after <= now()
   ORDER BY priority, run_after
   FOR UPDATE SKIP LOCKED
   LIMIT :n
)
UPDATE app.work_items w
   SET state='RUNNING', locked_by=:worker, locked_at=now(), attempts=w.attempts+1
  FROM claimed c
 WHERE w.id = c.id
RETURNING w.id, w.kind, w.payload, w.attempts, w.max_attempts
""")


async def enqueue(conn, kind: str, payload: dict[str, Any] | None = None,
                  dedupe_key: str | None = None, priority: int = 100) -> int | None:
    row = (await conn.execute(text("""
        INSERT INTO app.work_items (kind, payload, dedupe_key, priority)
        VALUES (:k, CAST(:p AS jsonb), :d, :pri)
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL
          AND state IN ('PENDING','RUNNING') DO NOTHING
        RETURNING id"""),
        {"k": kind, "p": json.dumps(payload or {}), "d": dedupe_key, "pri": priority})).scalar()
    return row


async def claim(conn, worker: str, n: int = 1):
    return (await conn.execute(CLAIM, {"worker": worker, "n": n})).all()


async def complete(conn, item_id: int) -> None:
    await conn.execute(text(
        "UPDATE app.work_items SET state='DONE', locked_by=NULL, locked_at=NULL WHERE id=:i"),
        {"i": item_id})


async def fail(conn, item_id: int, err: str, attempts: int, max_attempts: int) -> None:
    dead = attempts >= max_attempts
    await conn.execute(text("""
        UPDATE app.work_items
           SET state = :s, last_error = :e, locked_by = NULL, locked_at = NULL,
               run_after = now() + (interval '1 minute' * power(3, LEAST(:a, 5)))
         WHERE id = :i"""),
        {"s": "DEAD" if dead else "PENDING", "e": err[:2000], "a": attempts, "i": item_id})


async def reap_stale(conn, older_than_minutes: int = 30) -> int:
    """A worker killed mid-item leaves RUNNING forever. Return it to PENDING."""
    return (await conn.execute(text("""
        UPDATE app.work_items
           SET state='PENDING', locked_by=NULL, locked_at=NULL
         WHERE state='RUNNING' AND locked_at < now() - (interval '1 minute' * :m)
        RETURNING id"""), {"m": older_than_minutes})).rowcount
PYEOF
```

The `ON CONFLICT ... WHERE` clause must match the partial index predicate from migration 0001
exactly, or Postgres raises `there is no unique or exclusion constraint matching`.

## 6.4 Worker loop

```bash
cat > src/job_agent/workers/loop.py <<'PYEOF'
from __future__ import annotations

import asyncio
import json
import os
import signal

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.core.queue import claim, complete, fail, reap_stale
from job_agent.workers.discovery import poll_source

WORKER = os.environ.get("WORKER_NAME", "worker-1")
_stop = asyncio.Event()

HANDLERS = {
    "poll_source": lambda p: poll_source(p["source"]),
}


async def beat(detail: dict) -> None:
    async with tx() as conn:
        await conn.execute(text(
            "INSERT INTO app.heartbeats (worker, last_beat, detail) "
            "VALUES (:w, now(), CAST(:d AS jsonb)) "
            "ON CONFLICT (worker) DO UPDATE SET last_beat=now(), detail=EXCLUDED.detail"),
            {"w": WORKER, "d": json.dumps(detail)})


async def run_once() -> int:
    async with tx() as conn:
        items = await claim(conn, WORKER, n=1)
    if not items:
        return 0
    item = items[0]
    try:
        handler = HANDLERS.get(item.kind)
        if handler is None:
            raise ValueError(f"no handler for kind {item.kind!r}")
        await handler(item.payload)
        async with tx() as conn:
            await complete(conn, item.id)
        return 1
    except Exception as exc:
        async with tx() as conn:
            await fail(conn, item.id, f"{type(exc).__name__}: {exc}",
                       item.attempts, item.max_attempts)
        print(f"item {item.id} ({item.kind}) failed: {exc}")
        return 1


async def main() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, _stop.set)

    tick = 0
    while not _stop.is_set():
        tick += 1
        if tick % 30 == 0:
            async with tx() as conn:
                n = await reap_stale(conn)
                if n:
                    print(f"reaped {n} stale RUNNING items")
        did = await run_once()
        await beat({"idle": did == 0})
        if not did:
            try:
                await asyncio.wait_for(_stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
    print("worker stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
PYEOF
```

Change the worker service command:

```yaml
    command: ["python", "-m", "job_agent.workers.loop"]
```

## 6.5 Scheduler

```bash
cat > src/job_agent/scheduler/main.py <<'PYEOF'
from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from job_agent.core.config import get_settings
from job_agent.core.db import tx
from job_agent.core.queue import enqueue

SYDNEY = ZoneInfo("Australia/Sydney")
logging.basicConfig(level=logging.INFO)


async def enqueue_poll(source: str) -> None:
    """Scheduler ONLY enqueues. It never fetches."""
    async with tx() as conn:
        wid = await enqueue(conn, "poll_source", {"source": source},
                            dedupe_key=f"poll:{source}", priority=50)
    logging.info("enqueued poll %s -> %s", source, wid or "deduped")


async def daily_summary() -> None:
    async with tx() as conn:
        await enqueue(conn, "daily_summary", {}, dedupe_key=None, priority=200)


def build() -> AsyncIOScheduler:
    s = get_settings()
    sched = AsyncIOScheduler(
        jobstores={"default": SQLAlchemyJobStore(
            url=s.sync_database_url, tablename="apscheduler_jobs",
            tableschema="app")},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
        timezone=SYDNEY,
    )
    sched.add_job(enqueue_poll, CronTrigger(minute="*/30", timezone=SYDNEY),
                  args=["greenhouse"], id="poll:greenhouse", jitter=90, replace_existing=True)
    sched.add_job(enqueue_poll, CronTrigger(minute="7,37", timezone=SYDNEY),
                  args=["lever"], id="poll:lever", jitter=90, replace_existing=True)
    sched.add_job(enqueue_poll, CronTrigger(minute="*/30", timezone=SYDNEY),
                  args=["ashby"], id="poll:ashby", jitter=90, replace_existing=True)
    sched.add_job(daily_summary, CronTrigger(hour=19, minute=0, timezone=SYDNEY),
                  id="daily_summary", replace_existing=True)
    return sched


async def main() -> None:
    sched = build()
    sched.start()
    async with tx() as conn:
        await conn.execute(text(
            "INSERT INTO app.heartbeats (worker, last_beat) VALUES ('scheduler', now()) "
            "ON CONFLICT (worker) DO UPDATE SET last_beat = now()"))
    logging.info("scheduler running: %s", [j.id for j in sched.get_jobs()])
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
PYEOF
```

```yaml
  scheduler:
    build: .
    restart: unless-stopped
    mem_limit: 256m
    memswap_limit: 256m
    cpus: 0.5
    pids_limit: 128
    init: true
    read_only: true
    tmpfs: [/tmp:size=32m]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    environment:
      TZ: Australia/Sydney
      POSTGRES_USER: ja_scheduler
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_HOST: postgres
      WORKER_NAME: scheduler
    secrets: [ja_scheduler_password]
    depends_on:
      postgres: {condition: service_healthy}
    command: ["python", "-m", "job_agent.scheduler.main"]
    logging: *default-logging
    networks: [internal]
```

The scheduler container's `TZ` is `Australia/Sydney` — it is a scheduling boundary. Everything
else stays UTC.

## 6.6 Verification

**P6-T1 — SKIP LOCKED under concurrency**

```bash
docker compose up -d worker scheduler
docker compose run --rm worker python - <<'PY'
import asyncio
from job_agent.core.db import tx
from job_agent.core.queue import enqueue, claim

async def main():
    async with tx() as c:
        for i in range(20):
            await enqueue(c, "noop", {"i": i})
    async def grab(name):
        async with tx() as c:
            return [r.id for r in await claim(c, name, n=5)]
    got = await asyncio.gather(*[grab(f"w{i}") for i in range(4)])
    flat = [i for g in got for i in g]
    print("claimed:", sorted(flat))
    print("overlaps:", len(flat) - len(set(flat)))
asyncio.run(main())
PY
```

**Right:** 20 distinct ids, `overlaps: 0`. **Wrong:** any overlap means two workers would
process the same job — check the `FOR UPDATE SKIP LOCKED` is inside the CTE, not on the
UPDATE.

**P6-T2 — Dedupe key prevents double-enqueue**

```bash
docker compose run --rm worker python - <<'PY'
import asyncio
from job_agent.core.db import tx
from job_agent.core.queue import enqueue
async def main():
    async with tx() as c:
        a = await enqueue(c, "poll_source", {"source":"lever"}, dedupe_key="poll:lever")
        b = await enqueue(c, "poll_source", {"source":"lever"}, dedupe_key="poll:lever")
    print("first:", a, "second:", b)
asyncio.run(main())
PY
```

**Right:** first an id, second `None`. **Wrong:** two ids — the partial index predicate and
the `ON CONFLICT ... WHERE` don't match.

**P6-T3 — The schedule persists**

```bash
docker compose logs --tail=5 scheduler
make psql-ro
```
```sql
SELECT id, to_timestamp(next_run_time) AT TIME ZONE 'Australia/Sydney' AS next_sydney
  FROM app.apscheduler_jobs ORDER BY next_run_time;
```

**Right:** four jobs with sensible Sydney times. **Wrong:** empty table — the jobstore
`tableschema="app"` is missing and APScheduler tried to create its own table in `public`,
which `ja_scheduler` has no DDL rights for.

**P6-T4 — Restart resumes rather than resets**

```bash
docker compose restart scheduler && sleep 15
docker compose logs --tail=3 scheduler
```

**Right:** `scheduler running: ['poll:greenhouse', ...]` with the same next-run times as
before. **Wrong:** next-run times reset to now+interval means `replace_existing` is
overwriting state on each boot rather than reusing it.

**P6-T5 — Coalescing after an outage**

```bash
docker compose stop scheduler
sleep 120                      # miss at least two 30-minute-boundary... use a short cron to test
docker compose start scheduler && sleep 20
docker compose logs --tail=20 scheduler | grep -c 'enqueued poll'
```

**Right:** at most one catch-up enqueue per source, not one per missed interval. The
`dedupe_key` makes this true even if `coalesce` misbehaves — that redundancy is deliberate.

**P6-T6 — Stale claim reaping**

```bash
docker compose run --rm worker python - <<'PY'
import asyncio
from sqlalchemy import text
from job_agent.core.db import tx
from job_agent.core.queue import reap_stale
async def main():
    async with tx() as c:
        await c.execute(text("UPDATE app.work_items SET state='RUNNING', locked_by='dead', "
                             "locked_at = now() - interval '2 hours' WHERE state='PENDING'"))
        n = await reap_stale(c)
    print("reaped:", n)
asyncio.run(main())
PY
```

**Right:** a non-zero count. A worker OOM-killed mid-item must not strand the item forever.

**P6-T7 — Clean shutdown**

```bash
docker compose stop worker
docker compose logs --tail=3 worker
```

**Right:** `worker stopped cleanly` — the SIGTERM handler ran. **Wrong:** no message and a
10-second delay means Docker had to SIGKILL it, and an in-flight item was stranded.

## 6.7 Common errors

**`there is no unique or exclusion constraint matching the ON CONFLICT specification`**
The `WHERE` clause in the upsert must be character-identical to the partial index predicate.

**`permission denied for table apscheduler_jobs`**
`make grants` wasn't re-run, or `ja_scheduler` is missing `INSERT, UPDATE, DELETE`.

**Scheduler fires at the wrong hour**
Container `TZ` is not `Australia/Sydney`, or a `CronTrigger` is missing `timezone=SYDNEY`.
Both are required — the scheduler-level timezone doesn't apply to triggers built explicitly.

**Items stuck in `RUNNING`**
`reap_stale` isn't being called. It runs every 30 ticks in the loop.

## 6.8 Checklist

- [ ] 4 concurrent claimers, 0 overlaps (**P6-T1**)
- [ ] Dedupe key blocks a second enqueue (**P6-T2**)
- [ ] `apscheduler_jobs` holds 4 jobs with Sydney-correct next runs (**P6-T3**)
- [ ] Restart preserves the schedule (**P6-T4**)
- [ ] An outage produces one catch-up per source (**P6-T5**)
- [ ] Stale `RUNNING` items are reaped (**P6-T6**)
- [ ] Worker shuts down cleanly on SIGTERM (**P6-T7**)
- [ ] `docker stats`: worker < 1024m, scheduler < 256m
- [ ] **[PVE host]** `pct snapshot 120 phase6-complete`

---

# PHASE 7 — Deduplication

## 7.1 Objective

Resolve postings to `job_groups` via the five-layer key ladder. Applications attach to the
group, never the posting, so the same job on four sites produces one application.

## 7.2 Simhash and company matching

```bash
cat > src/job_agent/pipeline/simhash.py <<'PYEOF'
from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[a-z0-9]{3,}")


def _shingles(text: str, k: int = 3) -> list[str]:
    words = _TOKEN.findall(text.lower())
    if len(words) < k:
        return words
    return [" ".join(words[i:i + k]) for i in range(len(words) - k + 1)]


def simhash(text: str | None, bits: int = 64) -> int | None:
    if not text:
        return None
    shingles = _shingles(text)
    if not shingles:
        return None
    v = [0] * bits
    for sh in shingles:
        h = int.from_bytes(hashlib.blake2b(sh.encode(), digest_size=8).digest(), "big")
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= 1 << i
    return out - (1 << 63) if out >= (1 << 63) else out   # fit signed BIGINT


def hamming(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return 64
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")
PYEOF
```

Postgres `bigint` is signed; a 64-bit unsigned simhash overflows it. The wrap on the last line
is what keeps `content_simhash` insertable, and `hamming` masks back to 64 bits so the
comparison is unaffected.

```bash
cat > src/job_agent/pipeline/companies.py <<'PYEOF'
from __future__ import annotations

import re

SUFFIXES = r"\b(pty|ltd|limited|inc|incorporated|group|holdings|australia|au|corp|plc)\b"


def normalise_company(name: str) -> str:
    n = name.lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(SUFFIXES, " ", n)
    return re.sub(r"\s+", " ", n).strip()
PYEOF
```

## 7.3 The key ladder

```bash
cat > src/job_agent/pipeline/dedupe.py <<'PYEOF'
"""Five-layer key ladder. Layers 1-3 are confirmed; 4-5 are probable and reversible."""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.pipeline.simhash import hamming, simhash

FIND_L2 = text("""
SELECT gp.job_group_id FROM app.postings p
  JOIN app.job_group_postings gp ON gp.posting_id = p.id
 WHERE p.ats_platform = :platform AND p.ats_tenant = :tenant AND p.ats_job_id = :jid
   AND p.id <> :self LIMIT 1""")

FIND_L3 = text("""
SELECT gp.job_group_id FROM app.postings p
  JOIN app.job_group_postings gp ON gp.posting_id = p.id
 WHERE p.canonical_url = :curl AND p.id <> :self LIMIT 1""")

FIND_L4 = text("""
SELECT gp.job_group_id FROM app.postings p
  JOIN app.job_group_postings gp ON gp.posting_id = p.id
 WHERE p.company_id = :cid AND p.title_norm = :tnorm
   AND p.location_class = :loc AND p.id <> :self LIMIT 1""")

FIND_L5 = text("""
SELECT gp.job_group_id, p.content_simhash, p.description
  FROM app.postings p
  JOIN app.job_group_postings gp ON gp.posting_id = p.id
 WHERE p.company_id = :cid
   AND p.location_class = :loc
   AND similarity(p.title_norm, :tnorm) > 0.85
   AND p.id <> :self
 LIMIT 20""")


async def resolve(conn, posting_id: int) -> tuple[int, int, bool]:
    """Return (job_group_id, match_layer, probable). Idempotent per posting."""
    p = (await conn.execute(text("""
        SELECT id, company_id, title, title_norm, location_class, canonical_url,
               ats_platform, ats_tenant, ats_job_id, description, content_simhash
          FROM app.postings WHERE id = :i"""), {"i": posting_id})).first()
    if p is None:
        raise LookupError(f"no posting {posting_id}")

    existing = (await conn.execute(text(
        "SELECT job_group_id FROM app.job_group_postings WHERE posting_id = :i"),
        {"i": posting_id})).scalar()
    if existing:
        return existing, 0, False

    if p.content_simhash is None and p.description:
        sh = simhash(p.description)
        await conn.execute(text("UPDATE app.postings SET content_simhash=:s WHERE id=:i"),
                           {"s": sh, "i": posting_id})
    else:
        sh = p.content_simhash

    gid, layer, probable = None, 0, False

    if p.ats_platform and p.ats_job_id:
        gid = (await conn.execute(FIND_L2, {"platform": p.ats_platform, "tenant": p.ats_tenant,
                                            "jid": p.ats_job_id, "self": p.id})).scalar()
        layer = 2 if gid else 0

    if gid is None and p.canonical_url:
        gid = (await conn.execute(FIND_L3, {"curl": p.canonical_url, "self": p.id})).scalar()
        layer = 3 if gid else 0

    if gid is None and p.company_id:
        gid = (await conn.execute(FIND_L4, {"cid": p.company_id, "tnorm": p.title_norm,
                                            "loc": p.location_class, "self": p.id})).scalar()
        if gid:
            layer, probable = 4, True

    if gid is None and p.company_id and sh is not None:
        for row in (await conn.execute(FIND_L5, {"cid": p.company_id, "tnorm": p.title_norm,
                                                 "loc": p.location_class, "self": p.id})).all():
            if hamming(sh, row.content_simhash) <= 3:
                gid, layer, probable = row.job_group_id, 5, True
                break

    if gid is None:
        gid = (await conn.execute(text("""
            INSERT INTO app.job_groups (canonical_posting_id, company_id, title, location_class)
            VALUES (:p, :c, :t, :l) RETURNING id"""),
            {"p": p.id, "c": p.company_id, "t": p.title, "l": p.location_class})).scalar()
        layer = 1

    await conn.execute(text("""
        INSERT INTO app.job_group_postings (job_group_id, posting_id, match_layer, probable_duplicate)
        VALUES (:g, :p, :m, :d) ON CONFLICT (posting_id) DO NOTHING"""),
        {"g": gid, "p": p.id, "m": layer, "d": probable})

    await conn.execute(text("UPDATE app.postings SET state='DEDUPED' WHERE id=:i"), {"i": p.id})
    await conn.execute(text("""
        UPDATE app.job_groups SET last_seen_at = now() WHERE id = :g"""), {"g": gid})
    await conn.execute(text(
        "INSERT INTO app.events (entity_type, entity_id, actor, action, detail) "
        "VALUES ('job_group', :g, 'dedupe', :a, CAST(:d AS jsonb))"),
        {"g": gid, "a": "group_created" if layer == 1 else "posting_merged",
         "d": json.dumps({"posting_id": p.id, "layer": layer, "probable": probable})})
    return gid, layer, probable


async def unmerge(conn, posting_id: int, actor: str = "dashboard") -> int:
    """Detach a posting into its own group. Reversible by re-running resolve()."""
    old = (await conn.execute(text(
        "SELECT job_group_id FROM app.job_group_postings WHERE posting_id=:p"),
        {"p": posting_id})).scalar()
    await conn.execute(text("DELETE FROM app.job_group_postings WHERE posting_id=:p"),
                       {"p": posting_id})
    p = (await conn.execute(text(
        "SELECT company_id, title, location_class FROM app.postings WHERE id=:p"),
        {"p": posting_id})).first()
    gid = (await conn.execute(text("""
        INSERT INTO app.job_groups (canonical_posting_id, company_id, title, location_class)
        VALUES (:p,:c,:t,:l) RETURNING id"""),
        {"p": posting_id, "c": p.company_id, "t": p.title, "l": p.location_class})).scalar()
    await conn.execute(text("""
        INSERT INTO app.job_group_postings (job_group_id, posting_id, match_layer, merged_by)
        VALUES (:g,:p,1,:a)"""), {"g": gid, "p": posting_id, "a": actor})
    await conn.execute(text(
        "INSERT INTO app.events (entity_type, entity_id, actor, action, detail) "
        "VALUES ('job_group', :g, :a, 'unmerged', CAST(:d AS jsonb))"),
        {"g": gid, "a": actor, "d": json.dumps({"posting_id": posting_id, "from_group": old})})
    return gid
PYEOF
```

Layer 1 in the ladder — `(source_id, source_job_id)` — is enforced by a unique constraint at
insert time, so by the time a posting reaches `resolve()` it has already been applied. Layer 1
in `match_layer` therefore means "started a new group".

## 7.4 Wire into the worker

```bash
cat > src/job_agent/pipeline/run.py <<'PYEOF'
from __future__ import annotations

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.pipeline.dedupe import resolve


async def dedupe_pending(limit: int = 200) -> dict:
    counts = {"new_groups": 0, "merged": 0, "probable": 0}
    async with tx() as conn:
        ids = [r.id for r in (await conn.execute(text(
            "SELECT id FROM app.postings WHERE state = 'NORMALISED' ORDER BY id LIMIT :n"),
            {"n": limit})).all()]
    for pid in ids:
        async with tx() as conn:
            _, layer, probable = await resolve(conn, pid)
        if layer == 1:
            counts["new_groups"] += 1
        else:
            counts["merged"] += 1
        if probable:
            counts["probable"] += 1
    return counts
PYEOF
```

Add to `HANDLERS` in `src/job_agent/workers/loop.py`:

```python
from job_agent.pipeline.run import dedupe_pending

HANDLERS = {
    "poll_source": lambda p: poll_source(p["source"]),
    "dedupe": lambda p: dedupe_pending(p.get("limit", 200)),
}
```

And in `src/job_agent/scheduler/main.py`, after each poll job:

```python
    sched.add_job(lambda: _enqueue_kind("dedupe"), CronTrigger(minute="*/10", timezone=SYDNEY),
                  id="dedupe", replace_existing=True)
```

with:

```python
async def _enqueue_kind(kind: str) -> None:
    async with tx() as conn:
        await enqueue(conn, kind, {}, dedupe_key=kind, priority=80)
```

## 7.5 Verification

**P7-T1 — Simhash sanity**

```bash
docker compose run --rm worker python - <<'PY'
from job_agent.pipeline.simhash import simhash, hamming
a = simhash("We are hiring a graduate software engineer to work on distributed systems in Sydney")
b = simhash("We are hiring a graduate software engineer to work on distributed systems in Sydney.")
c = simhash("Senior marketing manager wanted for a Melbourne consumer brand agency role")
print("near-identical:", hamming(a,b), "(expect 0-3)")
print("unrelated:", hamming(a,c), "(expect > 15)")
print("fits bigint:", -(2**63) <= a < 2**63)
PY
```

**Right:** near-identical ≤ 3, unrelated > 15, `fits bigint: True`.
**Wrong:** `fits bigint: False` — the insert will fail with `bigint out of range`.

**P7-T2 — Real dedup run**

```bash
docker compose run --rm worker python -c "
import asyncio; from job_agent.pipeline.run import dedupe_pending
print(asyncio.run(dedupe_pending()))"
```

Then:

```sql
SELECT count(*) AS groups FROM app.job_groups;
SELECT match_layer, count(*), sum(probable_duplicate::int) AS probable
  FROM app.job_group_postings GROUP BY 1 ORDER BY 1;
SELECT count(*) FROM app.postings WHERE state='NORMALISED';
```

**Right:** most postings at layer 1 on a first run (one group each), `NORMALISED` count drops
to 0. **Wrong:** groups > postings means `job_group_postings` isn't being written; layer 4/5
counts dominating on a first run means the layer-4 key is too loose — check `location_class`
isn't `UNKNOWN` for everything, which collapses unrelated roles at the same company.

**P7-T3 — Idempotence**

```bash
docker compose run --rm worker python -c "
import asyncio; from job_agent.pipeline.run import dedupe_pending
print(asyncio.run(dedupe_pending()))"
```

**Right:** all zeros — nothing left in `NORMALISED`. Re-running `resolve()` on an
already-grouped posting must return its existing group, not create a second.

**P7-T4 — Cross-source collapse**

The real test. Add the same company under two platforms if it publishes on both, or simulate:

```bash
DB=$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$(grep '^POSTGRES_USER=' .env | cut -d= -f2)
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB" <<'SQL'
-- clone an existing posting under a different source, same ATS key
INSERT INTO app.postings (source_id, source_job_id, company_id, title, title_norm, description,
  location_class, ats_platform, ats_tenant, ats_job_id, canonical_url, url_hash, state)
SELECT (SELECT id FROM app.sources WHERE key='imap'), 'clone-1', company_id, title, title_norm,
       description, location_class, ats_platform, ats_tenant, ats_job_id,
       canonical_url || '?from=email', repeat('b',64), 'NORMALISED'
  FROM app.postings WHERE ats_job_id IS NOT NULL LIMIT 1;
SQL

docker compose run --rm worker python -c "
import asyncio; from job_agent.pipeline.run import dedupe_pending
print(asyncio.run(dedupe_pending()))"
```

```sql
SELECT match_layer, probable_duplicate FROM app.job_group_postings
 WHERE posting_id = (SELECT id FROM app.postings WHERE source_job_id='clone-1');
```

**Right:** `match_layer = 2`, `probable_duplicate = false`. The ATS-native key collapsed a
posting from a different source into the existing group with no guesswork. That is the layer
architecture §10 calls the highest-value key, and this test is why.

**P7-T5 — Unmerge is reversible**

```bash
docker compose run --rm worker python - <<'PY'
import asyncio
from sqlalchemy import text
from job_agent.core.db import tx
from job_agent.pipeline.dedupe import unmerge, resolve
async def main():
    async with tx() as c:
        pid = (await c.execute(text("SELECT posting_id FROM app.job_group_postings "
                                    "WHERE match_layer > 1 LIMIT 1"))).scalar()
        if pid is None: print("no merged posting to test"); return
        new = await unmerge(c, pid)
        print("unmerged", pid, "into group", new)
asyncio.run(main())
PY
```

```sql
SELECT action, detail FROM app.events WHERE action IN ('unmerged','posting_merged')
 ORDER BY at DESC LIMIT 4;
```

**Right:** the unmerge is recorded in `events` with the old group id, so it is auditable and
you can put it back.

## 7.6 Common errors

**`bigint out of range` on `content_simhash`**
The signed-range wrap in `simhash()` was removed or edited.

**`function similarity(text, text) does not exist`**
`pg_trgm` isn't installed, or `search_path` doesn't include `public`. Both are set in Phase 3
§5.2 — re-run `make bootstrap`.

**Everything merges into one group**
`location_class` is `UNKNOWN` for all postings (Phase 8 fixes that) and `title_norm` is
over-stripped. Until Phase 8 lands, restrict layer 4 to postings with a known location class.

**Nothing ever merges**
`company_id` is NULL on most postings — the slug in `config/companies.yaml` doesn't match
`app.companies.slug`.

## 7.7 Checklist

- [ ] Simhash: near-identical ≤ 3, unrelated > 15, fits bigint (**P7-T1**)
- [ ] Dedup run empties the `NORMALISED` queue (**P7-T2**)
- [ ] Re-running is a no-op (**P7-T3**)
- [ ] Cross-source posting collapses at layer 2 (**P7-T4**)
- [ ] Unmerge works and is recorded in `events` (**P7-T5**)
- [ ] `dedupe` job appears in `apscheduler_jobs`
- [ ] **[PVE host]** `pct snapshot 120 phase7-complete`

---

# PHASE 8 — Australian Filtering and Classification

## 8.1 Objective

Rule-based location, seniority, salary, work-rights and clearance classification. No LLM. All
unit-tested. This is the hard screen that kills ~80% of jobs before a token is spent.

## 8.2 Location

```bash
mkdir -p src/job_agent/screening
cat > src/job_agent/screening/location.py <<'PYEOF'
from __future__ import annotations

import re

SYDNEY_METRO = (2000, 2249)
CENTRAL_COAST = (2250, 2263)
NSW_OTHER = (2264, 2999)

CBD_SUBURBS = {"sydney", "sydney cbd", "haymarket", "barangaroo", "the rocks",
               "millers point", "darling harbour", "circular quay", "pyrmont"}
METRO_SUBURBS = {"north sydney", "chatswood", "parramatta", "macquarie park", "st leonards",
                 "surry hills", "redfern", "alexandria", "mascot", "ryde", "bondi junction",
                 "rhodes", "olympic park", "sydney olympic park", "north ryde", "epping",
                 "burwood", "strathfield", "hurstville", "manly", "eastwood", "ultimo"}
CAPITALS = {"melbourne": "VIC", "brisbane": "QLD", "perth": "WA", "adelaide": "SA",
            "hobart": "TAS", "darwin": "NT", "canberra": "ACT"}

REMOTE = re.compile(r"\b(fully remote|100% remote|work from home|wfh|remote[- ]first)\b", re.I)
HYBRID = re.compile(r"\bhybrid\b", re.I)
DAYS_IN_OFFICE = re.compile(r"(\d)\s*(?:days?)\s*(?:per week\s*)?(?:in|at)\s*(?:the\s*)?office", re.I)
POSTCODE = re.compile(r"\b(2\d{3})\b")


def classify(location_raw: str | None, description: str | None = None) -> tuple[str, int | None]:
    """Return (location_class, days_in_office)."""
    blob = f"{location_raw or ''} {description or ''}"
    low = (location_raw or "").lower().strip()

    days = None
    m = DAYS_IN_OFFICE.search(blob)
    if m:
        days = int(m.group(1))

    if not low and not description:
        return "UNKNOWN", days

    remote = bool(REMOTE.search(blob))
    hybrid = bool(HYBRID.search(blob))

    in_sydney = (
        any(s in low for s in CBD_SUBURBS | METRO_SUBURBS)
        or "sydney" in low or "nsw" in low
    )
    pc = POSTCODE.search(blob)
    if pc:
        code = int(pc.group(1))
        if SYDNEY_METRO[0] <= code <= SYDNEY_METRO[1]:
            in_sydney = True

    if hybrid and in_sydney:
        return "HYBRID_SYDNEY", days
    if remote:
        if "australia" in blob.lower() or "au only" in blob.lower() or in_sydney:
            return "REMOTE_AU", days
        return "REMOTE_GLOBAL", days

    for suburb in CBD_SUBURBS:
        if suburb in low:
            return "SYDNEY_CBD", days
    for suburb in METRO_SUBURBS:
        if suburb in low:
            return "SYDNEY_METRO", days

    if pc:
        code = int(pc.group(1))
        if SYDNEY_METRO[0] <= code <= SYDNEY_METRO[1]:
            return "SYDNEY_METRO", days
        if CENTRAL_COAST[0] <= code <= NSW_OTHER[1]:
            return "NSW_REGIONAL", days

    if "sydney" in low:
        return "SYDNEY_METRO", days
    if "nsw" in low:
        return "GREATER_SYDNEY", days

    for city in CAPITALS:
        if city in low:
            return "OTHER_CAPITAL", days

    if low and "australia" not in low:
        return "REQUIRES_RELOCATION", days
    return "UNKNOWN", days
PYEOF
```

## 8.3 Seniority

```bash
cat > src/job_agent/screening/seniority.py <<'PYEOF'
from __future__ import annotations

import re

TARGET = re.compile(
    r"\b(graduate|grad program|new grad|early career(s)?|emerging talent|junior|"
    r"entry[- ]level|trainee|cadet|intern(ship)?|campus hire|l1|se1|"
    r"software engineer i(?![iv]))\b", re.I)

SENIOR_NOUNS = r"(director|partner|principal|manager|lead|architect|consultant|vice president|vp)"
NEGATIVE = re.compile(
    rf"\b(senior|staff|principal|head of|director|manager|architect|"
    rf"l[4-9]|sme|lead\b)\b|\bassociate\s+{SENIOR_NOUNS}\b", re.I)

YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)\b", re.I)


def classify(title: str, description: str | None = None) -> tuple[str, int | None, bool]:
    """Return (seniority_class, min_years, conflict)."""
    t = title or ""
    negative = bool(NEGATIVE.search(t))
    target = bool(TARGET.search(t))

    if re.search(r"\bintern(ship)?\b", t, re.I) and not negative:
        cls = "INTERN"
    elif re.search(r"\b(graduate|grad program|new grad|campus hire|cadet)\b", t, re.I) and not negative:
        cls = "GRADUATE"
    elif target and not negative:
        cls = "JUNIOR"
    elif negative:
        cls = "SENIOR" if not re.search(r"\b(head of|director|vp)\b", t, re.I) else "LEAD"
    else:
        cls = "UNKNOWN"

    min_years = None
    if description:
        hits = [int(m.group(1)) for m in YEARS.finditer(description)]
        if hits:
            min_years = min(hits)

    conflict = cls in ("INTERN", "GRADUATE", "JUNIOR") and (min_years or 0) >= 4
    return cls, min_years, conflict
PYEOF
```

`\bassociate\s+(director|partner|...)` is the trap architecture §6.2 names: "Associate
Director" is senior, "Associate Software Engineer" is not.

## 8.4 Salary

```bash
cat > src/job_agent/screening/salary.py <<'PYEOF'
from __future__ import annotations

import re

MONEY = r"\$?\s*(\d{2,3}(?:[,\.]\d{3})*|\d{2,3})\s*(k\b)?"
RANGE = re.compile(rf"{MONEY}\s*(?:-|–|to)\s*{MONEY}", re.I)
SINGLE = re.compile(MONEY, re.I)
DAILY = re.compile(r"\$?\s*(\d{3,4})\s*(?:/|per\s*)?\s*(?:day|d\b|daily)", re.I)
HOURLY = re.compile(r"\$?\s*(\d{2,3}(?:\.\d{2})?)\s*(?:/|per\s*)?\s*(?:hour|hr\b|hourly)", re.I)
PLUS_SUPER = re.compile(r"\+\s*super", re.I)
PACKAGE = re.compile(r"\b(package|inclusive of super|incl\.? super|total remuneration|TRP)\b", re.I)
OTE = re.compile(r"\bOTE\b", re.I)

WORKING_DAYS = 220
WEEK_HOURS = 38
DEFAULT_SUPER = 0.12


def _num(raw: str, k: str | None) -> int:
    v = float(raw.replace(",", "").replace(".", "") if "," in raw or (
        "." in raw and len(raw.split(".")[-1]) == 3) else raw.replace(",", ""))
    if k:
        v *= 1000
    if v < 1000:
        v *= 1000
    return int(v)


def parse(text: str | None, super_rate: float = DEFAULT_SUPER) -> dict:
    out = {
        "base_salary_min": None, "base_salary_max": None, "super_included": None,
        "super_rate": super_rate, "total_package_min": None, "ote": None,
        "currency": "AUD", "period": "ANNUAL", "confidence": "ABSENT",
    }
    if not text:
        return out

    if m := DAILY.search(text):
        rate = int(m.group(1))
        out.update(period="DAILY", base_salary_min=rate * WORKING_DAYS,
                   confidence="INFERRED", super_included=False)
        out["total_package_min"] = int(out["base_salary_min"] * (1 + super_rate))
        return out

    if m := HOURLY.search(text):
        rate = float(m.group(1))
        out.update(period="HOURLY", base_salary_min=int(rate * WEEK_HOURS * 46),
                   confidence="INFERRED", super_included=False)
        out["total_package_min"] = int(out["base_salary_min"] * (1 + super_rate))
        return out

    lo = hi = None
    if m := RANGE.search(text):
        lo, hi = _num(m.group(1), m.group(2)), _num(m.group(3), m.group(4))
    elif m := SINGLE.search(text):
        v = _num(m.group(1), m.group(2))
        if 30000 <= v <= 500000:
            lo = v

    if lo is None:
        return out

    included = bool(PACKAGE.search(text)) and not PLUS_SUPER.search(text)
    out.update(base_salary_min=lo, base_salary_max=hi, super_included=included,
               confidence="STATED")
    out["total_package_min"] = lo if included else int(lo * (1 + super_rate))
    if OTE.search(text):
        out["ote"] = hi or lo
        out["base_salary_min"] = None if hi is None else lo
    return out
PYEOF
```

`total_package_min` is what the scorer compares, never the raw base. An `$80k package` role
pays less than a `$78k + super` role, and comparing base to package silently inverts them.

## 8.5 Work rights and clearance

```bash
cat > src/job_agent/screening/work_rights.py <<'PYEOF'
from __future__ import annotations

import re

MUST_CITIZEN = re.compile(
    r"\b(must be an? australian citizen|australian citizenship (is )?(required|mandatory)|"
    r"citizens? only|restricted to australian citizens)\b", re.I)
MUST_PR = re.compile(r"\b(must hold (permanent residency|pr)|permanent residents? only)\b", re.I)
NO_SPONSOR = re.compile(
    r"\b(no sponsorship|sponsorship (is )?not (available|offered|provided)|"
    r"unable to sponsor|we do not sponsor|no visa holders)\b", re.I)
SPONSOR_OK = re.compile(
    r"\b(sponsorship (is )?available|will sponsor|482 (visa )?available|"
    r"visa sponsorship (offered|available))\b", re.I)
FULL_RIGHTS = re.compile(
    r"\b(full working rights|unrestricted working rights|valid working rights|"
    r"full work rights|permanent working rights)\b", re.I)
PREFERRED = re.compile(r"\b(citizens? or (permanent residents?|pr) preferred|pr preferred)\b", re.I)

PROFILE_MEETS_CITIZEN = {"CITIZEN"}
PROFILE_MEETS_PR = {"CITIZEN", "PR"}
PROFILE_HAS_RIGHTS = {"CITIZEN", "PR", "TEMP_GRADUATE_485", "STUDENT_500"}


def classify(text: str | None, profile_status: str) -> tuple[str, str | None]:
    """Return (classification, clause_verbatim)."""
    if not text:
        return "UNCERTAIN", None

    def clause(m) -> str:
        s = max(0, m.start() - 100)
        return text[s:m.end() + 100].strip()

    if m := MUST_CITIZEN.search(text):
        if profile_status not in PROFILE_MEETS_CITIZEN:
            return "INCOMPATIBLE", clause(m)
        return "COMPATIBLE", clause(m)

    if m := MUST_PR.search(text):
        if profile_status not in PROFILE_MEETS_PR:
            return "INCOMPATIBLE", clause(m)
        return "COMPATIBLE", clause(m)

    if m := NO_SPONSOR.search(text):
        if profile_status == "REQUIRES_SPONSORSHIP":
            return "INCOMPATIBLE", clause(m)
        return "COMPATIBLE", clause(m)

    if m := SPONSOR_OK.search(text):
        return "SPONSORSHIP_POSSIBLE", clause(m)

    if m := PREFERRED.search(text):
        return "LIKELY_COMPATIBLE", clause(m)

    if m := FULL_RIGHTS.search(text):
        if profile_status in PROFILE_HAS_RIGHTS:
            return "COMPATIBLE", clause(m)
        return "INCOMPATIBLE", clause(m)

    return "UNCERTAIN", None
PYEOF

cat > src/job_agent/screening/clearance.py <<'PYEOF'
from __future__ import annotations

import re

TERMS = {
    "Baseline": r"\bbaseline\b",
    "NV1": r"\bnv1\b|\bnegative vetting 1\b",
    "NV2": r"\bnv2\b|\bnegative vetting 2\b",
    "PV": r"\bpositive vetting\b|\bpv clearance\b",
    "TSPV": r"\btspv\b",
    "AGSVA": r"\bagsva\b",
    "GENERIC": r"\b(defence clearance|australian government security clearance|security clearance)\b",
}
MUST_HOLD = re.compile(r"\b(must (currently )?hold|current(ly)? (hold|holding)|active clearance)\b", re.I)
OBTAINABLE = re.compile(r"\b(must be able to obtain|willing(ness)? to obtain|eligible to obtain)\b", re.I)
SPONSORED = re.compile(r"\b(agsva sponsorship provided|we will sponsor.{0,30}clearance|clearance will be sponsored)\b", re.I)
PREFERRED = re.compile(r"\b(clearance (is )?(desirable|preferred|advantageous)|highly regarded)\b", re.I)


def classify(text: str | None, profile_citizenship: str) -> tuple[list[str], str, str | None, bool]:
    """Return (detected, strength, clause, hard_block)."""
    if not text:
        return [], "UNCLEAR", None, False
    detected = [k for k, pat in TERMS.items() if re.search(pat, text, re.I)]
    if not detected:
        return [], "UNCLEAR", None, False

    m = re.search(TERMS[detected[0]], text, re.I)
    clause = text[max(0, m.start() - 120):m.end() + 120].strip() if m else None

    if SPONSORED.search(text):
        strength = "SPONSORED_AFTER_HIRE"
    elif MUST_HOLD.search(text):
        strength = "MANDATORY_HELD"
    elif OBTAINABLE.search(text):
        strength = "MANDATORY_OBTAINABLE"
    elif PREFERRED.search(text):
        strength = "PREFERRED"
    else:
        strength = "UNCLEAR"

    # AGSVA clearances require Australian citizenship, full stop.
    hard_block = strength.startswith("MANDATORY") and profile_citizenship != "AU"
    return detected, strength, clause, hard_block
PYEOF
```

## 8.6 The hard screen

```bash
cat > src/job_agent/pipeline/screen.py <<'PYEOF'
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.core.db import tx
from job_agent.screening import clearance, location, salary, seniority


async def _profile(conn) -> dict:
    row = (await conn.execute(text("""
        SELECT f.kind, f.data FROM app.profile_facts f
          JOIN app.profile_versions v ON v.id = f.profile_version_id
         WHERE v.is_active AND f.kind IN ('work_rights','preferences')"""))).all()
    out = {}
    for r in row:
        out[r.kind] = r.data if isinstance(r.data, dict) else json.loads(r.data)
    return out


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

    await conn.execute(text("""
        INSERT INTO app.work_right_assessments (job_group_id, classification, clause_verbatim, decided_by)
        VALUES (:g,:c,:v,'RULE')"""), {"g": gid, "c": wr_class, "v": wr_clause})
    if cl_detected:
        await conn.execute(text("""
            INSERT INTO app.clearance_assessments (job_group_id, detected, strength, clause_verbatim, decided_by)
            VALUES (:g,:d,:s,:v,'RULE')"""),
            {"g": gid, "d": cl_detected, "s": cl_strength, "v": cl_clause})

    # ---- hard gates, in order of certainty ----
    if wr_class == "INCOMPATIBLE":
        return "SCREENED_OUT", "WORK_RIGHTS_INCOMPATIBLE"
    if cl_block:
        return "SCREENED_OUT", "CLEARANCE_CITIZENSHIP_CONFLICT"
    if sen_class in ("SENIOR", "LEAD"):
        return "SCREENED_OUT", "SENIORITY_TOO_HIGH"
    accepted = set(prefs.get("location_classes_accepted", []))
    if accepted and loc_class not in accepted and loc_class != "UNKNOWN":
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
    if wr_class == "UNCERTAIN":
        return "REVIEW_REQUIRED", "WORK_RIGHTS_UNCERTAIN"
    return "READY", None


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
PYEOF
```

Add `"screen": lambda p: screen_pending(p.get("limit", 200))` to `HANDLERS`, and a scheduler
job on `*/10`.

## 8.7 Unit tests

```bash
cat > tests/unit/test_screening.py <<'PYEOF'
import pytest
from job_agent.screening import clearance, location, salary, seniority


@pytest.mark.parametrize("raw,expected", [
    ("Sydney, NSW", "SYDNEY_METRO"),
    ("Barangaroo, Sydney", "SYDNEY_CBD"),
    ("Melbourne, VIC", "OTHER_CAPITAL"),
    ("Newcastle NSW 2300", "NSW_REGIONAL"),
    ("Remote - Australia", "REMOTE_AU"),
    ("London, UK", "REQUIRES_RELOCATION"),
    ("", "UNKNOWN"),
])
def test_location(raw, expected):
    assert location.classify(raw)[0] == expected


def test_hybrid_days():
    cls, days = location.classify("Sydney - hybrid", "3 days in the office each week")
    assert cls == "HYBRID_SYDNEY" and days == 3


@pytest.mark.parametrize("title,expected", [
    ("Graduate Software Engineer", "GRADUATE"),
    ("Software Engineer Intern", "INTERN"),
    ("Junior Developer", "JUNIOR"),
    ("Senior Software Engineer", "SENIOR"),
    ("Associate Director, Risk", "SENIOR"),
    ("Associate Software Engineer", "JUNIOR"),
    ("Head of Engineering", "LEAD"),
])
def test_seniority(title, expected):
    assert seniority.classify(title)[0] == expected


def test_seniority_conflict():
    cls, yrs, conflict = seniority.classify(
        "Graduate Engineer", "We require 5+ years of production experience")
    assert cls == "GRADUATE" and yrs == 5 and conflict is True


@pytest.mark.parametrize("text,base,pkg_gt_base", [
    ("$70,000 + super", 70000, True),
    ("$70k plus superannuation", 70000, True),
    ("$80k package", 80000, False),
    ("$90,000 - $110,000", 90000, True),
])
def test_salary(text, base, pkg_gt_base):
    s = salary.parse(text)
    assert s["base_salary_min"] == base
    assert (s["total_package_min"] > base) == pkg_gt_base


def test_salary_daily_flagged():
    s = salary.parse("$650/day contract rate")
    assert s["period"] == "DAILY" and s["confidence"] == "INFERRED"


def test_package_vs_plus_super_ordering():
    a = salary.parse("$80k package")["total_package_min"]
    b = salary.parse("$78,000 + super")["total_package_min"]
    assert b > a, "a package figure must not outrank a higher base + super"


@pytest.mark.parametrize("text,status,expected", [
    ("Must be an Australian citizen", "TEMP_GRADUATE_485", "INCOMPATIBLE"),
    ("Must be an Australian citizen", "CITIZEN", "COMPATIBLE"),
    ("No sponsorship available", "REQUIRES_SPONSORSHIP", "INCOMPATIBLE"),
    ("Sponsorship available for the right candidate", "REQUIRES_SPONSORSHIP", "SPONSORSHIP_POSSIBLE"),
    ("Citizens or PR preferred", "TEMP_GRADUATE_485", "LIKELY_COMPATIBLE"),
    ("Great team, great culture", "CITIZEN", "UNCERTAIN"),
])
def test_work_rights(text, status, expected):
    from job_agent.screening.work_rights import classify
    assert classify(text, status)[0] == expected


def test_clearance_citizenship_block():
    _, strength, _, block = clearance.classify(
        "Must currently hold an NV1 clearance", "IN")
    assert strength == "MANDATORY_HELD" and block is True
    _, _, _, block_au = clearance.classify(
        "Must currently hold an NV1 clearance", "AU")
    assert block_au is False


def test_clearance_preferred_never_blocks():
    _, strength, _, block = clearance.classify(
        "A security clearance is desirable", "IN")
    assert strength == "PREFERRED" and block is False
PYEOF

cat >> requirements.txt <<'EOF'
pytest==8.3.4
pytest-asyncio==0.25.0
EOF

cat >> Makefile <<'EOF'
.PHONY: test
test: ; docker compose run --rm worker python -m pytest tests/unit -q
EOF
```

Mount tests into the worker service:

```yaml
      - ./tests:/app/tests:ro
```

## 8.8 Verification

**P8-T1 — Unit tests**

```bash
docker compose build worker
make test
```

**Right:** all pass. **Wrong:** `test_package_vs_plus_super_ordering` failing means the
super calculation is inverted, which would systematically favour worse-paying roles.

**P8-T2 — Screen the real corpus**

```bash
docker compose run --rm worker python -c "
import asyncio; from job_agent.pipeline.screen import screen_pending
print(asyncio.run(screen_pending()))"
```

```sql
SELECT status, screen_reason, count(*) FROM app.job_groups GROUP BY 1,2 ORDER BY 3 DESC;
SELECT location_class, count(*) FROM app.postings GROUP BY 1 ORDER BY 2 DESC;
SELECT seniority_class, count(*) FROM app.postings GROUP BY 1 ORDER BY 2 DESC;
```

**Right:** a large `SCREENED_OUT` share (architecture expects ~80%), mostly
`SENIORITY_TOO_HIGH` and location reasons. `UNKNOWN` under 20% for both classes.
**Wrong:** everything `READY` — the profile isn't loaded, so `prefs` is empty and no gate
fires. Check `SELECT count(*) FROM app.profile_versions WHERE is_active`.

**P8-T3 — Nothing auto-rejects for ambiguity**

```sql
SELECT classification, count(*) FROM app.work_right_assessments GROUP BY 1;
SELECT count(*) FROM app.job_groups g
  JOIN app.work_right_assessments w ON w.job_group_id = g.id
 WHERE w.classification = 'UNCERTAIN' AND g.status = 'SCREENED_OUT';
```

**Right:** the second query returns 0. `UNCERTAIN` routes to `REVIEW_REQUIRED`, never to a
rejection — a large share of AU ads simply omit the clause, and auto-rejecting them deletes
most of your feed.

**P8-T4 — Spot-check the screened-out pile**

```sql
SELECT g.screen_reason, p.title, p.location_raw
  FROM app.job_groups g JOIN app.postings p ON p.id = g.canonical_posting_id
 WHERE g.status = 'SCREENED_OUT' ORDER BY random() LIMIT 15;
```

Read all fifteen. **Right:** every one is a job you would genuinely not apply for. **Wrong:**
any role you would have wanted — that is a false negative, and false negatives are invisible
in production. Fix the rule and re-run before moving on.

## 8.9 Common errors

**Everything is `UNKNOWN` location**
`location_raw` is NULL because the adapter didn't map the field. Check
`SELECT location_raw FROM app.postings LIMIT 5`.

**`SENIORITY_TOO_HIGH` on graduate roles**
The negative regex matched a word in the title like "Lead" in "Lead Generation". Tighten with
a word boundary or a negative lookahead.

**Salary parsed as 70 instead of 70000**
The `k` suffix group didn't match. `_num` multiplies anything under 1000 by 1000 as a
backstop, but check the regex.

**`KeyError: 'work_rights'`**
The profile isn't loaded. Run `make profile-load`.

## 8.10 Checklist

- [ ] `make test` all green (**P8-T1**)
- [ ] Screening classifies the real corpus, `UNKNOWN` under 20% (**P8-T2**)
- [ ] No `UNCERTAIN` work-rights group was screened out (**P8-T3**)
- [ ] 15 random screened-out roles manually reviewed, no false negatives (**P8-T4**)
- [ ] `screen` job in `apscheduler_jobs`
- [ ] **[PVE host]** `pct snapshot 120 phase8-complete`

---

# PHASE 9 — Dashboard v1

## 9.1 Objective

Overview, job feed, job detail, pipeline board and event timeline. Jinja2 + HTMX, no build
step, one container. You need to see the pipeline to debug it.

## 9.2 Templates

```bash
mkdir -p src/job_agent/web/templates src/job_agent/web/static

cat > src/job_agent/web/templates/base.html <<'EOF'
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}Job Agent{% endblock %}</title>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<style>
:root{--bg:#0f1115;--fg:#e6e8ec;--mut:#8b93a1;--card:#171a21;--line:#252a34;
      --ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:12px 20px;border-bottom:1px solid var(--line);display:flex;gap:20px;align-items:center}
header a{color:var(--mut);text-decoration:none}header a.on{color:var(--fg)}
main{padding:20px;max-width:1100px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
.card .n{font-size:28px;font-weight:600}
.card .l{color:var(--mut);font-size:13px}
table{width:100%;border-collapse:collapse}
td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:13px}
.pill{padding:2px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--mut)}
a{color:#58a6ff}
.dry{background:var(--warn);color:#000;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600}
</style>
</head>
<body>
<header>
  <strong>job-agent</strong>
  <a href="/" class="{% if page=='overview' %}on{% endif %}">Overview</a>
  <a href="/jobs" class="{% if page=='jobs' %}on{% endif %}">Jobs</a>
  <a href="/pipeline" class="{% if page=='pipeline' %}on{% endif %}">Pipeline</a>
  <a href="/events" class="{% if page=='events' %}on{% endif %}">Events</a>
  <span style="margin-left:auto">
    {% if dry_run %}<span class="dry">DRY RUN</span>{% endif %}
  </span>
</header>
<main>{% block body %}{% endblock %}</main>
</body></html>
EOF

cat > src/job_agent/web/templates/overview.html <<'EOF'
{% extends "base.html" %}{% block body %}
<div class="grid" hx-get="/partials/stats" hx-trigger="every 30s" hx-swap="outerHTML">
  {% include "_stats.html" %}
</div>
<h3>Workers</h3>
<table><tr><th>Worker</th><th>Last beat</th><th>Age</th></tr>
{% for w in heartbeats %}
<tr><td>{{ w.worker }}</td><td>{{ w.last_beat_sydney }}</td>
    <td class="{{ 'bad' if w.age_s > 900 else 'ok' }}">{{ w.age_s }}s</td></tr>
{% else %}<tr><td colspan="3" class="mut">no heartbeats yet</td></tr>{% endfor %}
</table>
<h3>Sources</h3>
<table><tr><th>Source</th><th>Circuit</th><th>Failures</th><th>Postings</th></tr>
{% for s in sources %}
<tr><td>{{ s.key }}</td>
    <td class="{{ 'bad' if s.circuit_state=='OPEN' else 'ok' }}">{{ s.circuit_state }}</td>
    <td>{{ s.consecutive_failures }}</td><td>{{ s.postings }}</td></tr>
{% endfor %}
</table>
{% endblock %}
EOF

cat > src/job_agent/web/templates/_stats.html <<'EOF'
<div class="grid" hx-get="/partials/stats" hx-trigger="every 30s" hx-swap="outerHTML">
{% for s in stats %}
  <div class="card"><div class="n">{{ s.value }}</div><div class="l">{{ s.label }}</div></div>
{% endfor %}
</div>
EOF

cat > src/job_agent/web/templates/jobs.html <<'EOF'
{% extends "base.html" %}{% block body %}
<form hx-get="/partials/jobs" hx-target="#feed" hx-trigger="change,keyup delay:400ms">
  <select name="status">
    <option value="">all</option>
    {% for s in ['READY','REVIEW_REQUIRED','DISCOVERED','SCREENED_OUT','APPLIED','CLOSED'] %}
    <option value="{{ s }}" {% if s==status %}selected{% endif %}>{{ s }}</option>{% endfor %}
  </select>
  <input name="q" ="title contains…" value="{{ q or '' }}">
</form>
<div id="feed">{% include "_jobs.html" %}</div>
{% endblock %}
EOF

cat > src/job_agent/web/templates/_jobs.html <<'EOF'
<table>
<tr><th>Title</th><th>Company</th><th>Location</th><th>Posted</th><th>Status</th></tr>
{% for j in jobs %}
<tr>
  <td><a href="/jobs/{{ j.id }}">{{ j.title }}</a></td>
  <td>{{ j.company or '—' }}</td>
  <td>{{ j.location_class }}</td>
  <td class="mut">{{ j.posted_sydney or '—' }}</td>
  <td><span class="pill">{{ j.status }}</span>
      {% if j.screen_reason %}<div class="mut" style="font-size:12px">{{ j.screen_reason }}</div>{% endif %}</td>
</tr>
{% else %}<tr><td colspan="5" class="mut">nothing matches</td></tr>{% endfor %}
</table>
EOF

cat > src/job_agent/web/templates/job_detail.html <<'EOF'
{% extends "base.html" %}{% block body %}
<h2>{{ job.title }}</h2>
<p class="mut">{{ job.company or '—' }} · {{ job.location_class }} ·
   posted {{ job.posted_sydney or 'unknown' }} ({{ job.posted_at_confidence }})</p>
<p><span class="pill">{{ job.status }}</span>
   {% if job.screen_reason %}<span class="pill bad">{{ job.screen_reason }}</span>{% endif %}</p>

<h3>Sources ({{ postings|length }})</h3>
<table><tr><th>Source</th><th>Layer</th><th>Dup?</th><th>URL</th></tr>
{% for p in postings %}
<tr><td>{{ p.source }}</td><td>{{ p.match_layer }}</td>
    <td>{{ 'probable' if p.probable_duplicate else '—' }}</td>
    <td>{% if p.canonical_url %}<a href="{{ p.canonical_url }}" rel="noreferrer">open</a>{% endif %}</td></tr>
{% endfor %}
</table>

{% if work_rights %}
<h3>Work rights — {{ work_rights.classification }}</h3>
{% if work_rights.clause_verbatim %}<blockquote class="mut">{{ work_rights.clause_verbatim }}</blockquote>{% endif %}
{% endif %}
{% if clearance %}
<h3>Clearance — {{ clearance.strength }}</h3>
{% if clearance.clause_verbatim %}<blockquote class="mut">{{ clearance.clause_verbatim }}</blockquote>{% endif %}
{% endif %}
{% if salary and salary.confidence != 'ABSENT' %}
<h3>Salary</h3>
<p>base {{ salary.base_salary_min or '?' }}{% if salary.base_salary_max %}–{{ salary.base_salary_max }}{% endif %}
   · package {{ salary.total_package_min }} · {{ salary.confidence }} · {{ salary.period }}</p>
{% endif %}

<h3>Timeline</h3>
<table><tr><th>When</th><th>Actor</th><th>Action</th><th>Detail</th></tr>
{% for e in events %}
<tr><td class="mut">{{ e.at_sydney }}</td><td>{{ e.actor }}</td><td>{{ e.action }}</td>
    <td class="mut">{{ e.detail }}</td></tr>
{% endfor %}
</table>

<h3>Description</h3>
<pre style="white-space:pre-wrap">{{ description or '' }}</pre>
{% endblock %}
EOF

cat > src/job_agent/web/templates/pipeline.html <<'EOF'
{% extends "base.html" %}{% block body %}
<div hx-get="/partials/pipeline" hx-trigger="every 15s" hx-swap="innerHTML">
{% include "_pipeline.html" %}
</div>
{% endblock %}
EOF

cat > src/job_agent/web/templates/_pipeline.html <<'EOF'
<div class="grid">
{% for c in columns %}
<div class="card"><div class="n">{{ c.count }}</div><div class="l">{{ c.label }}</div></div>
{% endfor %}
</div>
<h3>Queue</h3>
<table><tr><th>Kind</th><th>State</th><th>Count</th><th>Oldest</th></tr>
{% for w in work %}
<tr><td>{{ w.kind }}</td><td class="{{ 'bad' if w.state in ('FAILED','DEAD') else '' }}">{{ w.state }}</td>
    <td>{{ w.count }}</td><td class="mut">{{ w.oldest_sydney or '—' }}</td></tr>
{% else %}<tr><td colspan="4" class="mut">queue empty</td></tr>{% endfor %}
</table>
EOF

cat > src/job_agent/web/templates/events.html <<'EOF'
{% extends "base.html" %}{% block body %}
<table hx-get="/partials/events" hx-trigger="every 20s" hx-swap="outerHTML">
<tr><th>When</th><th>Entity</th><th>Actor</th><th>Action</th><th>Detail</th></tr>
{% for e in events %}
<tr><td class="mut">{{ e.at_sydney }}</td>
    <td>{% if e.entity_type=='job_group' and e.entity_id %}<a href="/jobs/{{ e.entity_id }}">{{ e.entity_type }} {{ e.entity_id }}</a>{% else %}{{ e.entity_type }}{% endif %}</td>
    <td>{{ e.actor }}</td><td>{{ e.action }}</td><td class="mut">{{ e.detail }}</td></tr>
{% endfor %}
</table>
{% endblock %}
EOF
```

## 9.3 Routes

```bash
cat > src/job_agent/web/routes.py <<'PYEOF'
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from job_agent.core.config import get_settings
from job_agent.core.db import engine

SYD = ZoneInfo("Australia/Sydney")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
settings = get_settings()


def syd(dt: datetime | None) -> str | None:
    """The ONLY place UTC becomes Sydney. Storage and computation stay UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(SYD).strftime("%Y-%m-%d %H:%M")


async def q(sql: str, **params):
    async with engine().connect() as c:
        return (await c.execute(text(sql), params)).mappings().all()


def ctx(request: Request, page: str, **kw) -> dict:
    return {"request": request, "page": page, "dry_run": settings.dry_run, **kw}


async def _stats() -> list[dict]:
    rows = await q("""
      SELECT
        (SELECT count(*) FROM app.job_groups)                                   AS groups,
        (SELECT count(*) FROM app.job_groups WHERE status='READY')              AS ready,
        (SELECT count(*) FROM app.job_groups WHERE status='REVIEW_REQUIRED')    AS review,
        (SELECT count(*) FROM app.postings)                                     AS postings,
        (SELECT count(*) FROM app.job_groups
          WHERE first_seen_at > now() - interval '24 hours')                    AS today,
        (SELECT count(*) FROM app.work_items WHERE state IN ('PENDING','RUNNING')) AS queued
    """)
    r = rows[0]
    labels = [("groups", "job groups"), ("ready", "ready"), ("review", "needs review"),
              ("postings", "postings"), ("today", "new 24h"), ("queued", "queued")]
    return [{"label": lab, "value": r[k]} for k, lab in labels]


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    hb = [{**dict(h), "last_beat_sydney": syd(h["last_beat"]),
           "age_s": int((datetime.now(timezone.utc) - h["last_beat"]).total_seconds())}
          for h in await q("SELECT worker, last_beat FROM app.heartbeats ORDER BY worker")]
    src = await q("""
        SELECT s.key, s.circuit_state, s.consecutive_failures,
               (SELECT count(*) FROM app.postings p WHERE p.source_id = s.id) AS postings
          FROM app.sources s WHERE s.enabled ORDER BY s.key""")
    return templates.TemplateResponse("overview.html",
        ctx(request, "overview", stats=await _stats(), heartbeats=hb, sources=src))


@router.get("/partials/stats", response_class=HTMLResponse)
async def partial_stats(request: Request):
    return templates.TemplateResponse("_stats.html", ctx(request, "overview", stats=await _stats()))


JOBS_SQL = """
SELECT g.id, g.title, g.status, g.screen_reason, g.location_class,
       c.name AS company, p.posted_at, p.posted_at_confidence
  FROM app.job_groups g
  LEFT JOIN app.companies c ON c.id = g.company_id
  LEFT JOIN app.postings p ON p.id = g.canonical_posting_id
 WHERE (:status = '' OR g.status = :status)
   AND (:q = '' OR g.title ILIKE '%' || :q || '%')
 ORDER BY p.posted_at DESC NULLS LAST, g.id DESC
 LIMIT 100"""


async def _jobs(status: str, qq: str):
    rows = await q(JOBS_SQL, status=status or "", q=qq or "")
    return [{**dict(r), "posted_sydney": syd(r["posted_at"])} for r in rows]


@router.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, status: str = "", q: str = ""):
    return templates.TemplateResponse("jobs.html",
        ctx(request, "jobs", jobs=await _jobs(status, q), status=status, q=q))


@router.get("/partials/jobs", response_class=HTMLResponse)
async def partial_jobs(request: Request, status: str = "", q: str = ""):
    return templates.TemplateResponse("_jobs.html",
        ctx(request, "jobs", jobs=await _jobs(status, q)))


@router.get("/jobs/{gid}", response_class=HTMLResponse)
async def job_detail(request: Request, gid: int):
    job = (await q("""
        SELECT g.id, g.title, g.status, g.screen_reason, g.location_class,
               c.name AS company, p.posted_at, p.posted_at_confidence,
               p.description, p.salary
          FROM app.job_groups g
          LEFT JOIN app.companies c ON c.id = g.company_id
          LEFT JOIN app.postings p ON p.id = g.canonical_posting_id
         WHERE g.id = :g""", g=gid))
    if not job:
        return HTMLResponse("not found", status_code=404)
    j = {**dict(job[0]), "posted_sydney": syd(job[0]["posted_at"])}
    postings = await q("""
        SELECT s.key AS source, gp.match_layer, gp.probable_duplicate, p.canonical_url
          FROM app.job_group_postings gp
          JOIN app.postings p ON p.id = gp.posting_id
          JOIN app.sources s ON s.id = p.source_id
         WHERE gp.job_group_id = :g ORDER BY gp.match_layer""", g=gid)
    wr = await q("SELECT classification, clause_verbatim FROM app.work_right_assessments "
                 "WHERE job_group_id=:g ORDER BY created_at DESC LIMIT 1", g=gid)
    cl = await q("SELECT strength, clause_verbatim FROM app.clearance_assessments "
                 "WHERE job_group_id=:g ORDER BY created_at DESC LIMIT 1", g=gid)
    ev = [{**dict(e), "at_sydney": syd(e["at"]), "detail": json.dumps(e["detail"])[:200]}
          for e in await q("SELECT at, actor, action, detail FROM app.events "
                           "WHERE entity_type='job_group' AND entity_id=:g "
                           "ORDER BY at DESC LIMIT 50", g=gid)]
    return templates.TemplateResponse("job_detail.html",
        ctx(request, "jobs", job=j, postings=postings,
            work_rights=wr[0] if wr else None, clearance=cl[0] if cl else None,
            salary=j.get("salary"), events=ev, description=j.get("description")))


async def _pipeline():
    cols = await q("""
        SELECT status AS label, count(*) AS count FROM app.job_groups
         GROUP BY 1 ORDER BY 2 DESC""")
    work = [{**dict(w), "oldest_sydney": syd(w["oldest"])} for w in await q("""
        SELECT kind, state, count(*) AS count, min(created_at) AS oldest
          FROM app.work_items GROUP BY 1,2 ORDER BY 3 DESC""")]
    return cols, work


@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline(request: Request):
    cols, work = await _pipeline()
    return templates.TemplateResponse("pipeline.html", ctx(request, "pipeline", columns=cols, work=work))


@router.get("/partials/pipeline", response_class=HTMLResponse)
async def partial_pipeline(request: Request):
    cols, work = await _pipeline()
    return templates.TemplateResponse("_pipeline.html", ctx(request, "pipeline", columns=cols, work=work))


async def _events():
    return [{**dict(e), "at_sydney": syd(e["at"]), "detail": json.dumps(e["detail"])[:160]}
            for e in await q("SELECT at, entity_type, entity_id, actor, action, detail "
                             "FROM app.events ORDER BY at DESC LIMIT 200")]


@router.get("/events", response_class=HTMLResponse)
async def events(request: Request):
    return templates.TemplateResponse("events.html", ctx(request, "events", events=await _events()))


@router.get("/partials/events", response_class=HTMLResponse)
async def partial_events(request: Request):
    return templates.TemplateResponse("events.html", ctx(request, "events", events=await _events()))
PYEOF
```

Wire into `src/job_agent/web/app.py` — replace the `/` route:

```python
from job_agent.web.routes import router
...
app.include_router(router)
```

Keep `/health` exactly as it is; the Caddy `handle /health` block bypasses auth and Uptime
Kuma depends on it.

```bash
cat >> requirements.txt <<'EOF'
python-multipart==0.0.20
EOF
```

Add the templates to the api image — they live under `src/`, so the existing `COPY src/` covers
them. No Dockerfile change.

## 9.4 Verification

**P9-T1 — Pages render**

```bash
docker compose build api && docker compose up -d --force-recreate api && sleep 10
for p in / /jobs /pipeline /events; do
  printf '%-12s ' "$p"
  curl -sk -u admin:YOURPASS -o /dev/null -w '%{http_code}\n' "https://192.168.1.115$p"
done
```

**Right:** `200` four times. **Wrong:** `500` — check `docker compose logs api`; the usual
cause is a template referencing a key the route didn't pass.

**P9-T2 — Auth still holds**

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://192.168.1.115/jobs      # expect 401
curl -sk -o /dev/null -w '%{http_code}\n' https://192.168.1.115/health    # expect 200
```

**Right:** 401 then 200. The dashboard is authenticated; `/health` deliberately is not.
**Wrong:** `/jobs` returning 200 unauthenticated — the Caddy `handle` ordering broke.

**P9-T3 — Timezone at the boundary only**

```bash
curl -sk -u admin:YOURPASS https://192.168.1.115/jobs | grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}' | head -3
```

Compare against the stored value:

```sql
SELECT posted_at AS utc, posted_at AT TIME ZONE 'Australia/Sydney' AS sydney
  FROM app.postings ORDER BY posted_at DESC LIMIT 3;
```

**Right:** the page shows the Sydney column, 10 or 11 hours ahead of stored UTC.
**Wrong:** the page showing UTC means `syd()` isn't applied to that field — grep the route for
a raw datetime passed to a template.

**P9-T4 — HTMX polling works**

Open `https://job-agent.<tailnet>.ts.net/pipeline` on your phone. Leave it open 40 seconds.

**Right:** counts refresh without a page reload. **Wrong:** nothing updates — check the
browser console for a CSP or CDN block; the HTMX script comes from unpkg, which requires
Caddy egress (it has it) but also that your browser can reach it.

**P9-T5 — Detail page tells the story**

```bash
curl -sk -u admin:YOURPASS https://192.168.1.115/jobs/1 | grep -c 'Timeline'
```

Then open a screened-out job in a browser. **Right:** you can see the work-rights clause
verbatim, the match layer for each source, and an event timeline explaining why it was
screened out. That is the debugging surface the rest of the build depends on.

**P9-T6 — Footprint**

```bash
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'
free -h
```

**Right:** api under 300 MiB, total under 1.6 GiB with worker and scheduler running.
**Wrong:** api climbing on each page load — a connection leak; check `pg_stat_activity`.

## 9.5 Common errors

**`jinja2.exceptions.TemplateNotFound`**
`Jinja2Templates(directory=...)` resolves relative to `routes.py`. Confirm
`docker compose exec api ls /app/src/job_agent/web/templates`.

**`AttributeError: 'Row' object has no attribute 'get'`**
Use `.mappings().all()` so rows behave like dicts. The `q()` helper already does.

**All pages 401 including with the right password**
The `$$` escaping in `DASHBOARD_PASSWORD_HASH` — Phase 2 §11.

**HTMX partial replaces the whole page**
`hx-swap="outerHTML"` on a container whose partial re-renders the same container. `_stats.html`
includes its own wrapper deliberately; don't nest it inside another one.

**Timestamps off by an hour twice a year**
Something applied a fixed `+10:00` instead of the named zone. `syd()` is the only conversion
point; keep it that way.

## 9.6 Checklist

- [ ] All four pages return 200 authenticated (**P9-T1**)
- [ ] Unauthenticated `/jobs` is 401, `/health` is 200 (**P9-T2**)
- [ ] Displayed times are Sydney; stored times are UTC (**P9-T3**)
- [ ] HTMX polling updates without a reload, on your phone over Tailscale (**P9-T4**)
- [ ] Job detail shows sources, match layers, clauses and timeline (**P9-T5**)
- [ ] api under 300 MiB; stack total under 1.6 GiB (**P9-T6**)
- [ ] `DRY RUN` badge visible in the header
- [ ] **[PVE host]** `pct snapshot 120 phase9-complete`

---

## Report back

```
Phase 4 — facts loaded:        ______   PublicProfile leaks: ______
Phase 5 — tenants verified:    ______   postings fetched:    ______
          posted_at EXACT %:   ______
Phase 6 — SKIP LOCKED overlaps:______   scheduler jobs:      ______
Phase 7 — groups / postings:   ______ / ______
          layer 2 collapses:   ______
Phase 8 — screened out %:      ______   UNKNOWN location %:  ______
          false negatives in 15 sampled: ______
Phase 9 — idle stack total:    ______ MiB
Anything that failed:          ___________________
```

## What Phase 10 inherits

- A `notifications` outbox table, empty, with the dedupe index in place.
- `ja_notifier` granted and passworded, not yet mounted anywhere.
- A dashboard to deep-link Discord cards into.
- You need a Discord webhook URL in `/opt/job-agent/secrets/discord_webhook` before starting.

**Next:** Phase 10 — the notifier worker, card renderer, rate limiting and failure tests.
