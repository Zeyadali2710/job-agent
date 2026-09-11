-- postings_url_hash was UNIQUE, which contradicts the dedupe design it was
-- named after.
--
-- Layer 3 exists to merge two posting rows that share a canonical URL into one
-- job_group -- the same reasoning the comment on postings_ats_key already
-- states for layer 2: "two sources legitimately produce two posting rows for
-- one requisition. Collapsing them is the job_group's job." A UNIQUE
-- constraint collapses them a step earlier and in the wrong place: the second
-- INSERT raises a unique violation on a constraint that is NOT the
-- ON CONFLICT target in UPSERT_POSTING, so it surfaces as an unhandled
-- IntegrityError, is caught by discovery.py's per-record except, and the
-- posting is dropped with an insert_failed event. The row layer 3 exists to
-- merge can never be created.
--
-- Harmless with four ATS platforms, whose URL spaces are disjoint. It bites as
-- soon as Phase 10 adds a feed or email source pointing at an ATS URL already
-- being polled -- precisely the case layer 3 was written for.
--
-- Duplicate protection within one source is unaffected: that is
-- postings_source_key UNIQUE (source_id, source_job_id), which stays, as does
-- the url_hash CHECK.
ALTER TABLE app.postings DROP CONSTRAINT postings_url_hash;

-- Deliberately no replacement index on url_hash: the column is written by
-- discovery.py and read by nothing. Its unique index was the only thing
-- covering it, and it never served layer 3 anyway.
--
-- dedupe.FIND_L3 matches on canonical_url, which has never been indexed --
-- once the constraint above stops rejecting the rows, that lookup is the one
-- doing the work. Partial because layer 3 is skipped when canonical_url is
-- NULL (greenhouse nulls it when the job id is absent from absolute_url).
CREATE INDEX postings_canonical_url ON app.postings (canonical_url)
  WHERE canonical_url IS NOT NULL;
