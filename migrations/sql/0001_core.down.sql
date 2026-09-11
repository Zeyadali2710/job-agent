DROP TABLE IF EXISTS app.apscheduler_jobs, app.heartbeats, app.settings,
  app.work_items, app.events, app.job_group_postings, app.job_groups,
  app.posting_versions, app.postings, app.raw_postings, app.company_rules,
  app.company_aliases, app.companies, app.sources CASCADE;
DROP FUNCTION IF EXISTS app.deny_mutation();
DROP FUNCTION IF EXISTS app.set_updated_at();
