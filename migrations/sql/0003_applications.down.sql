DROP TABLE IF EXISTS app.application_rules, app.notifications,
  app.application_status_history, app.open_questions, app.application_answers,
  app.generated_documents, app.attempt_artifacts, app.application_attempts,
  app.applications CASCADE;
DROP FUNCTION IF EXISTS app.guard_dry_run();
