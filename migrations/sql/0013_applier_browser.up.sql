-- Phase 15: the applier drives a real form, so it needs three things it was
-- deliberately not given in Phase 14.

-- The identity a form asks for: name, email, phone, links. Exactly what the
-- employer receives anyway, which is the same reasoning PublicProfile rests
-- on. Note what is NOT granted: schema sensitive stays the renderer's alone,
-- so the applier can type your name into a form and still cannot read your
-- address, TFN or visa number.
GRANT SELECT ON app.profile_facts, app.profile_versions TO ja_applier;

-- automation_policy lives on the source. Architecture 5 says it is enforced
-- in code; code cannot enforce what it cannot read.
GRANT SELECT ON app.sources TO ja_applier;

INSERT INTO app.settings (key, value, descr) VALUES
  ('automation.adapter', '"mock"'::jsonb,
   'mock | browser. Which adapter the applier uses. "browser" opens a real '
   'Chromium against the employer''s form; "mock" writes the form it would '
   'have submitted to disk and touches nothing. Default mock: a browser '
   'loading an employer page is a visible act and should be deliberate.')
ON CONFLICT (key) DO NOTHING;
