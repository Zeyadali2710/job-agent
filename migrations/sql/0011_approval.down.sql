UPDATE app.settings SET value = '5'::jsonb
 WHERE key = 'generate.max_drafts_per_run';
