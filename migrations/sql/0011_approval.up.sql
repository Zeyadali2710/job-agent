-- Phase 13: approval workflow.
--
-- The cadence change in scheduler/main.py and this number are one decision.
-- At measured prices a cover letter is 0.026 AUD and an answer set 0.028, so
-- an application costs 0.054. Five a day is 8.1 AUD a month against a 7 AUD
-- cap; two a day is 3.2 and leaves room for enrichment.
UPDATE app.settings SET value = '2'::jsonb, descr =
  'Applications drafted per daily run. 0.054 AUD each -- this and the cron '
  'cadence together set the monthly bill.'
 WHERE key = 'generate.max_drafts_per_run';

-- Routing needs no new grants: ja_worker already has UPDATE on applications
-- and, since 0010, INSERT on application_status_history. Recorded here so the
-- absence is a decision rather than an oversight.
