-- Phase 11 configuration. These live in app.settings rather than in code so
-- they can be changed without a deploy -- the weights especially, which encode
-- a personal preference that is meant to be tuned after seeing real output.
--
-- ai.mode: 'on' | 'off'. The original architecture 9 offered 'local_only' as
-- the zero-cloud escape hatch, but the hardware revision removed local
-- inference entirely, so there is no local mode to fall back to. 'off' keeps
-- the intent: every deterministic stage still runs, nothing is sent anywhere.
--
-- ai.monthly_budget_aud: the in-code half of the spend cap. The revised
-- architecture 6 is blunt about why it exists -- "a retry loop against a paid
-- API is the only way this system can cost you real money". Set a hard cap in
-- the Anthropic console as well; that one cannot be defeated by a bug here.
--
-- score.weights: {} means use pipeline/score.py DEFAULT_WEIGHTS. Any override
-- changes the effective scorer version, which re-scores the corpus rather than
-- leaving stale scores behind.
INSERT INTO app.settings (key, value, descr) VALUES
  ('ai.mode', '"on"'::jsonb,
   'on | off -- off disables every model call, deterministic stages still run'),
  ('ai.monthly_budget_aud', '20'::jsonb,
   'Hard monthly cap in AUD; enrichment halts when month-to-date llm_calls reach it'),
  ('ai.fx_aud_per_usd', '1.55'::jsonb,
   'Conversion for llm_calls.cost_aud; API prices are published in USD'),
  ('score.weights', '{}'::jsonb,
   'Overrides for pipeline/score.py DEFAULT_WEIGHTS; {} uses the defaults')
ON CONFLICT (key) DO NOTHING;
