DELETE FROM app.settings WHERE key IN (
  'generate.min_score', 'generate.max_drafts_per_run', 'generate.artifact_dir');

REVOKE SELECT ON app.companies FROM ja_renderer;
REVOKE SELECT, INSERT, UPDATE ON app.work_items FROM ja_renderer;
REVOKE INSERT, UPDATE ON app.generated_documents, app.application_answers,
                         app.open_questions, app.application_status_history
  FROM ja_worker;

DROP INDEX IF EXISTS app.generated_documents_one_per_kind;
ALTER TABLE app.generated_documents DROP COLUMN IF EXISTS validator_findings;
ALTER TABLE app.generated_documents DROP COLUMN IF EXISTS content;
