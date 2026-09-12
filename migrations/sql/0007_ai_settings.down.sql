DELETE FROM app.settings
 WHERE key IN ('ai.mode', 'ai.monthly_budget_aud', 'ai.fx_aud_per_usd', 'score.weights');
