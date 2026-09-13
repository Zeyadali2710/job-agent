\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- broad read
GRANT SELECT ON ALL TABLES IN SCHEMA app TO ja_api, ja_worker, ja_readonly;

ALTER DEFAULT PRIVILEGES FOR ROLE ja_migrator IN SCHEMA app
  GRANT SELECT ON TABLES TO ja_api, ja_worker, ja_readonly;

-- ---------------------------------------------------------------- api
GRANT INSERT, UPDATE ON
  app.applications, app.application_answers, app.open_questions,
  app.application_status_history, app.application_rules, app.settings,
  app.company_rules, app.job_groups, app.notifications
  TO ja_api;
GRANT INSERT ON app.events TO ja_api;
-- Approving an application enqueues a submit item. INSERT only: the API
-- must never claim work, which needs UPDATE.
GRANT INSERT ON app.work_items TO ja_api;

-- ---------------------------------------------------------------- worker
GRANT INSERT, UPDATE ON
  app.postings, app.posting_versions, app.job_groups, app.job_group_postings,
  app.companies, app.company_aliases, app.job_analyses, app.job_scores,
  app.score_features, app.work_right_assessments, app.clearance_assessments,
  app.work_items, app.heartbeats, app.applications, app.sources,
  app.profile_versions, app.profile_facts
  TO ja_worker;
GRANT INSERT ON app.raw_postings, app.events, app.llm_calls, app.notifications TO ja_worker;
GRANT DELETE ON app.job_group_postings TO ja_worker;   -- unmerge, dashboard-initiated

-- ---------------------------------------------------------------- scheduler
GRANT SELECT ON app.sources, app.settings, app.job_groups TO ja_scheduler;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.apscheduler_jobs TO ja_scheduler;
GRANT SELECT, INSERT, UPDATE ON app.work_items, app.heartbeats TO ja_scheduler;
GRANT INSERT ON app.events TO ja_scheduler;

-- ---------------------------------------------------------------- notifier
-- Architecture §8: "Notifier has no profile read." Explicit list, no ALL TABLES.
GRANT SELECT ON app.notifications, app.job_groups, app.postings, app.companies,
                app.job_scores, app.score_features, app.settings TO ja_notifier;
GRANT UPDATE ON app.notifications TO ja_notifier;
GRANT INSERT ON app.events TO ja_notifier;
GRANT SELECT, UPDATE, INSERT ON app.heartbeats TO ja_notifier;

-- ---------------------------------------------------------------- renderer (Phase 12)
GRANT SELECT ON app.applications, app.job_groups, app.postings, app.job_analyses,
                app.profile_versions, app.profile_facts, app.application_answers,
                app.generated_documents, app.settings TO ja_renderer;
GRANT SELECT, INSERT, UPDATE ON app.generated_documents, app.application_answers TO ja_renderer;
GRANT INSERT ON app.events, app.open_questions TO ja_renderer;
GRANT SELECT, UPDATE, INSERT ON app.heartbeats TO ja_renderer;   -- SELECT: the beat is an upsert

-- The only sensitive grant in the system.
GRANT SELECT ON sensitive.profile_sensitive TO ja_renderer;
ALTER DEFAULT PRIVILEGES FOR ROLE ja_migrator IN SCHEMA sensitive
  GRANT SELECT ON TABLES TO ja_renderer;

-- ---------------------------------------------------------------- applier (Phases 14-15)
GRANT SELECT ON app.applications, app.application_attempts, app.attempt_artifacts,
                app.generated_documents, app.application_answers, app.open_questions,
                app.job_groups, app.postings, app.companies, app.company_rules,
                app.application_rules, app.settings, app.work_items TO ja_applier;
GRANT SELECT, INSERT, UPDATE ON app.application_attempts, app.attempt_artifacts TO ja_applier;
GRANT UPDATE ON app.applications TO ja_applier;
GRANT INSERT ON app.application_status_history, app.events, app.notifications TO ja_applier;
GRANT SELECT, UPDATE, INSERT ON app.work_items, app.heartbeats TO ja_applier;
-- Phase 15: the identity a form asks for, and the policy that says whether
-- the form may be driven at all. Schema sensitive stays the renderer's
-- alone -- the applier can type your name and still cannot read your
-- address, TFN or visa number.
GRANT SELECT ON app.profile_facts, app.profile_versions, app.sources TO ja_applier;

-- ---------------------------------------------------------------- hard denials
-- Belt and braces over the append-only triggers, and over anything ALL TABLES gave away.
REVOKE UPDATE, DELETE, TRUNCATE ON app.events, app.raw_postings, app.llm_calls,
       app.application_status_history
  FROM ja_api, ja_worker, ja_scheduler, ja_notifier, ja_renderer, ja_applier, ja_readonly;

-- No service role may ever reach the sensitive schema except the renderer.
REVOKE ALL ON SCHEMA sensitive
  FROM ja_api, ja_worker, ja_scheduler, ja_notifier, ja_applier, ja_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA sensitive
  FROM ja_api, ja_worker, ja_scheduler, ja_notifier, ja_applier, ja_readonly;

-- readonly is exactly that.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA app FROM ja_readonly;
