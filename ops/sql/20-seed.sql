\set ON_ERROR_STOP on
BEGIN;

INSERT INTO app.sources (key, kind, display_name, poll_cron, rate_budget_per_day, automation_policy) VALUES
  ('greenhouse',     'ATS',        'Greenhouse',      '*/30 * * * *', NULL, 'API_ONLY'),
  ('lever',          'ATS',        'Lever',           '7,37 * * * *', NULL, 'API_ONLY'),
  ('ashby',          'ATS',        'Ashby',           '*/30 * * * *', NULL, 'API_ONLY'),
  ('workable',       'ATS',        'Workable',        '*/30 * * * *', NULL, 'API_ONLY'),
  ('smartrecruiters','ATS',        'SmartRecruiters', '*/30 * * * *', NULL, 'API_ONLY'),
  ('workday',        'ATS',        'Workday',         '*/30 * * * *', NULL, 'API_ONLY'),
  ('pageup',         'ATS',        'PageUp',          '*/30 * * * *', NULL, 'API_ONLY'),
  ('personio',       'ATS',        'Personio',        '*/30 * * * *', NULL, 'API_ONLY'),
  ('adzuna',         'AGGREGATOR', 'Adzuna AU',       '13 */6 * * *',   30, 'API_ONLY'),
  ('imap',           'EMAIL',      'Email alerts',    '*/15 * * * *', NULL, 'MANUAL_ONLY')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app.settings (key, value, descr) VALUES
  ('dry_run',                  'true'::jsonb, 'Third-layer submit guard. False only at Phase 19.'),
  ('daily_application_cap',    '8'::jsonb,    'Global cap, architecture §13'),
  ('freshness_window_hours',   '24'::jsonb,   'Scoring input, never a hard gate (§0.2)'),
  ('reapply_policy',           '"never"'::jsonb, 'never | after_days'),
  ('ai_monthly_spend_cap_aud', '25'::jsonb,   'Halts enrichment when exceeded'),
  ('notify_min_score',         '80'::jsonb,   'HIGH_MATCH_JOB threshold')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app.application_rules (name, enabled, priority, match, action) VALUES
  ('default-require-approval', true, 1000, '{}'::jsonb, 'REQUIRE_APPROVAL')
ON CONFLICT (name) DO NOTHING;

INSERT INTO app.companies (name, slug, classification, website) VALUES
  ('Canva',          'canva',          'DIRECT_EMPLOYER', 'https://www.canva.com/careers/'),
  ('Atlassian',      'atlassian',      'DIRECT_EMPLOYER', 'https://www.atlassian.com/company/careers'),
  ('SafetyCulture',  'safetyculture',  'DIRECT_EMPLOYER', 'https://safetyculture.com/careers/'),
  ('Airwallex',      'airwallex',      'DIRECT_EMPLOYER', 'https://www.airwallex.com/careers'),
  ('WiseTech Global','wisetech-global','DIRECT_EMPLOYER', 'https://www.wisetechglobal.com/careers/')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('system', NULL, 'phase-3', 'schema_seeded',
        jsonb_build_object('revision', (SELECT version_num FROM app.alembic_version)));

COMMIT;
