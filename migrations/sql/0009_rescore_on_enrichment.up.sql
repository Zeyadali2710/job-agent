-- Scoring has to be able to run twice for one version now. Enrichment fills in
-- job_analyses.skills, which is the input to the scorer's skill_overlap
-- feature, and it necessarily lands after the first score: the hard screen and
-- the deterministic score both run before a single token is spent, by design.
-- Without a way to recompute, every enriched job would keep the score it was
-- given while skill_overlap was still neutral.
--
-- Recomputing means replacing that score's evidence rows, so ja_worker needs
-- DELETE on score_features -- the only table whose rows it owns outright and
-- could not previously replace. job_scores itself is updated in place via
-- ON CONFLICT, so the UNIQUE (job_group_id, scorer_version) history across
-- versions is untouched.
GRANT DELETE ON app.score_features TO ja_worker;

INSERT INTO app.settings (key, value, descr) VALUES
  ('notify.high_match_min_score', '80'::jsonb,
   'Architecture 7: HIGH_MATCH_JOB fires at or above this score')
ON CONFLICT (key) DO NOTHING;
