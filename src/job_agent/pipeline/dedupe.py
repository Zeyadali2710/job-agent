"""Five-layer key ladder. Layers 1-3 are confirmed; 4-5 are probable and reversible."""
from __future__ import annotations

import json

from sqlalchemy import text

from job_agent.pipeline.simhash import hamming, simhash
from job_agent.screening.location import classify as classify_location

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
SELECT jg.id FROM app.postings p
  JOIN app.job_groups jg ON jg.canonical_posting_id = p.id
 WHERE p.company_id = :cid AND p.title_norm = :tnorm
   AND p.location_class = :loc AND p.location_class <> 'UNKNOWN'
   AND (:loc NOT IN ('REQUIRES_RELOCATION','REMOTE_GLOBAL','OTHER_CAPITAL')
        OR similarity(coalesce(p.location_raw,''), coalesce(:loc_raw,'')) > 0.5)
   AND p.id <> :self LIMIT 1""")


FIND_L5 = text("""
SELECT jg.id AS job_group_id, p.content_simhash
  FROM app.postings p
  JOIN app.job_groups jg ON jg.canonical_posting_id = p.id
 WHERE p.company_id = :cid
   AND p.location_class = :loc AND p.location_class <> 'UNKNOWN'
   AND (:loc NOT IN ('REQUIRES_RELOCATION','REMOTE_GLOBAL','OTHER_CAPITAL')
        OR similarity(coalesce(p.location_raw,''), coalesce(:loc_raw,'')) > 0.5)
   AND similarity(p.title_norm, :tnorm) > 0.92
   AND p.id <> :self
 LIMIT 20""")



async def resolve(conn, posting_id: int) -> tuple[int, int, bool]:
    """Return (job_group_id, match_layer, probable). Idempotent per posting."""
    p = (await conn.execute(text("""
        SELECT id, company_id, title, title_norm, location_class, location_raw, canonical_url,
               ats_platform, ats_tenant, ats_job_id, description, content_simhash
          FROM app.postings WHERE id = :i"""), {"i": posting_id})).first()
    if p is None:
        raise LookupError(f"no posting {posting_id}")

    existing = (await conn.execute(text(
        "SELECT job_group_id FROM app.job_group_postings WHERE posting_id = :i"),
        {"i": posting_id})).scalar()
    if existing:
        return existing, 0, False

    loc_class_for_matching = p.location_class
    if p.location_class == 'UNKNOWN':
        loc_class, days = classify_location(
            (await conn.execute(text("SELECT location_raw FROM app.postings WHERE id=:i"),
                                {"i": posting_id})).scalar(),
            p.description)
        await conn.execute(text(
            "UPDATE app.postings SET location_class=:l, days_in_office=:d WHERE id=:i"),
            {"l": loc_class, "d": days, "i": posting_id})
        loc_class_for_matching = loc_class

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

    if gid is None and p.company_id and loc_class_for_matching != "UNKNOWN":
        gid = (await conn.execute(FIND_L4, {"cid": p.company_id, "tnorm": p.title_norm,
                                            "loc": loc_class_for_matching,
                                            "loc_raw": p.location_raw, "self": p.id})).scalar()
        if gid:
            layer, probable = 4, True

    if gid is None and p.company_id and sh is not None and loc_class_for_matching != 'UNKNOWN':
        for row in (await conn.execute(FIND_L5, {"cid": p.company_id, "tnorm": p.title_norm,
                                                 "loc": loc_class_for_matching,
                                                 "loc_raw": p.location_raw, "self": p.id})).all():
            if hamming(sh, row.content_simhash) <= 3:
                gid, layer, probable = row.job_group_id, 5, True
                break

    if gid is None:
        gid = (await conn.execute(text("""
            INSERT INTO app.job_groups (canonical_posting_id, company_id, title, location_class)
            VALUES (:p, :c, :t, :l) RETURNING id"""),
            {"p": p.id, "c": p.company_id, "t": p.title,
             # NOT p.location_class: p is the row snapshot taken before the
             # UNKNOWN reclassification above, so using it stamped the group
             # 'UNKNOWN' even when the posting had just been resolved.
             "l": loc_class_for_matching})).scalar()
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
