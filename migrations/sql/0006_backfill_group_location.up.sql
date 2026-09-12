-- job_groups.location_class is a denormalised copy of the canonical posting's
-- classification, and dedupe.resolve() stamped it from the row snapshot taken
-- BEFORE the UNKNOWN reclassification ran -- so essentially every group was
-- created carrying 'UNKNOWN' while its posting held the real value. The
-- dashboard reads the group's column, so every job displayed as UNKNOWN.
--
-- Screening was never affected: screen_group recomputes from location_raw on
-- every pass. Dedupe layers 4 and 5 were never affected either: they compare
-- app.postings.location_class, which was always correct.
--
-- The write is fixed in dedupe.py and screen.py now keeps the copy in step.
-- This repairs the rows already in the table.
UPDATE app.job_groups g
   SET location_class = p.location_class
  FROM app.postings p
 WHERE p.id = g.canonical_posting_id
   AND g.location_class = 'UNKNOWN'
   AND p.location_class <> 'UNKNOWN';
