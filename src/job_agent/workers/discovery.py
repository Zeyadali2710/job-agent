from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

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

ENSURE_COMPANY = text("""
INSERT INTO app.companies (name, slug, ats_platform, ats_tenant)
VALUES (:name, :slug, :platform, :tenant)
ON CONFLICT (slug) DO UPDATE SET
  ats_platform = EXCLUDED.ats_platform,
  ats_tenant = EXCLUDED.ats_tenant,
  updated_at = now()
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

# An unchanged payload makes INSERT_RAW return NULL, which used to skip
# UPSERT_POSTING entirely -- so last_seen_at froze at first sight and every
# still-live job looked stale the next day. "Still on the board" and "the
# payload changed" are different facts; only the second one needs the upsert.
TOUCH_POSTING = text("""
UPDATE app.postings p SET last_seen_at = now()
  FROM app.sources s
 WHERE s.id = p.source_id AND s.key = :skey AND p.source_job_id = :sjid
""")

# The group's own last_seen_at was only ever its insert-time default: resolve()
# returns early for an already-grouped posting, so nothing advanced it.
TOUCH_GROUP = text("""
UPDATE app.job_groups g SET last_seen_at = now()
  FROM app.job_group_postings gp, app.postings p, app.sources s
 WHERE g.id = gp.job_group_id AND p.id = gp.posting_id
   AND s.id = p.source_id AND s.key = :skey AND p.source_job_id = :sjid
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


NOTIFY_CIRCUIT_OPEN = text("""
INSERT INTO app.notifications (kind, dedupe_key, payload)
VALUES ('SYSTEM_ERROR', :key, CAST(:payload AS jsonb))
ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
""")


async def record_failure(conn, skey: str, err: str) -> None:
    row = (await conn.execute(text("""
        UPDATE app.sources
           SET consecutive_failures = consecutive_failures + 1,
               circuit_state = CASE WHEN consecutive_failures + 1 >= 3 THEN 'OPEN' ELSE circuit_state END,
               circuit_opened_at = CASE WHEN consecutive_failures + 1 >= 3 THEN now() ELSE circuit_opened_at END
         WHERE key = :k
     RETURNING circuit_state, consecutive_failures"""), {"k": skey})).first()
    await conn.execute(text(
        "INSERT INTO app.events (entity_type, actor, action, detail) "
        "VALUES ('source', :a, 'fetch_failed', CAST(:d AS jsonb))"),
        {"a": WORKER, "d": json.dumps({"source": skey, "error": err[:500]})})

    # Architecture 7: one SYSTEM_ERROR per circuit-open, not per failure. The
    # hour bucket in the dedupe key plus the partial unique index on
    # notifications.dedupe_key gives the "throttled 1/hour/class" for free, so
    # a flapping source cannot spam the channel.
    if row and row.circuit_state == "OPEN":
        await conn.execute(NOTIFY_CIRCUIT_OPEN, {
            "key": f"system_error:circuit_open:{skey}:"
                   f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')}",
            "payload": json.dumps({
                "title": f"Source circuit open: {skey}",
                "component": "discovery",
                "source": skey,
                "detail": f"{row.consecutive_failures} consecutive failures. "
                          f"Last error: {err[:300]}",
            }),
        })


async def poll_source(skey: str) -> tuple[int, int]:
    adapter = ADAPTERS[skey]
    companies = [c for c in load_companies() if c["platform"] == skey]
    new_raw = new_postings = 0

    for company in companies:
        tenant = company["tenant"]

        async with tx() as conn:
            await conn.execute(ENSURE_COMPANY, {
                "name": company.get("name", company["slug"]),
                "slug": company["slug"],
                "platform": skey,
                "tenant": tenant,
            })
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
            try:
                async with tx() as conn:
                    rid = (await conn.execute(INSERT_RAW, {
                        "skey": skey, "sjid": raw.source_job_id, "url": raw.url,
                        "payload": json.dumps(raw.payload, default=str),
                        "sha": raw.payload_sha256,
                    })).scalar()
                    if rid is None:
                        await conn.execute(TOUCH_POSTING,
                                           {"skey": skey, "sjid": raw.source_job_id})
                        await conn.execute(TOUCH_GROUP,
                                           {"skey": skey, "sjid": raw.source_job_id})
                        continue
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
                        "uhash": url_hash(
                            n.canonical_url if adapter.trust_url_for_hash else None,
                            f"{skey}:{n.source_job_id}",
                        ),
                        "etype": n.employment_type, "salary": json.dumps(n.salary),
                    })).first()
                    if res and res.inserted:
                        new_postings += 1
                    # UPSERT_POSTING advances the posting's own last_seen_at,
                    # never the group's.
                    await conn.execute(TOUCH_GROUP,
                                       {"skey": skey, "sjid": raw.source_job_id})
            except Exception as exc:
                async with tx() as conn:
                    await conn.execute(text(
                        "INSERT INTO app.events (entity_type, actor, action, detail) "
                        "VALUES ('posting', :a, 'insert_failed', CAST(:d AS jsonb))"),
                        {"a": WORKER, "d": json.dumps({
                            "source": skey, "tenant": tenant,
                            "source_job_id": raw.source_job_id,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        })})
                print(f"FAIL {skey}/{tenant}/{raw.source_job_id}: {type(exc).__name__}: {exc}", file=sys.stderr)

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
