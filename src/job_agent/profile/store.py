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
