DELETE FROM app.settings WHERE key = 'automation.adapter';
REVOKE SELECT ON app.sources FROM ja_applier;
REVOKE SELECT ON app.profile_facts, app.profile_versions FROM ja_applier;
