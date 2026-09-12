-- job_scores is deliberately append-only per version: UNIQUE (job_group_id,
-- scorer_version) keeps a record of what each scorer version concluded, and
-- ja_worker has no DELETE on score_features so a score cannot be rewritten in
-- place. Bumping the version -- or tuning score.weights, which folds into the
-- version -- therefore adds a row rather than replacing one.
--
-- That is correct, and it left a gap: nothing said which row is current. A
-- naive "SELECT ... FROM app.job_scores" counts every version at once, which
-- is exactly how the first re-score reported 140 scores for 70 jobs.
--
-- Latest-per-group rather than a filter on the current version string, so no
-- caller has to know what the scorer version happens to be today.
CREATE OR REPLACE VIEW app.current_job_scores AS
SELECT DISTINCT ON (job_group_id)
       id, job_group_id, base_score, llm_adjustment, score, band,
       scorer_version, created_at
  FROM app.job_scores
 ORDER BY job_group_id, created_at DESC, id DESC;

-- ja_api, ja_worker and ja_readonly would inherit this through the default
-- privileges 10-grants.sql sets for ja_migrator, but ja_notifier is on an
-- explicit allowlist (architecture 8: "Notifier has no profile read") and
-- would not. It needs the view to render a HIGH_MATCH_JOB card.
GRANT SELECT ON app.current_job_scores
  TO ja_api, ja_worker, ja_readonly, ja_notifier;
