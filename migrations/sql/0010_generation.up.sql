-- Phase 12: document generation.
--
-- generated_documents could record where a PDF ended up but not what it said.
-- The drafted blocks are the thing the provenance validator checked, the thing
-- the dashboard has to show you before you approve, and the thing the renderer
-- turns into a PDF -- so they belong in the row rather than in a work-item
-- payload that is deleted once the item completes.
ALTER TABLE app.generated_documents
  ADD COLUMN IF NOT EXISTS content jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Findings from a failed validation, so the quarantine says which entity or
-- number was unsupported instead of "validation failed".
ALTER TABLE app.generated_documents
  ADD COLUMN IF NOT EXISTS validator_findings jsonb NOT NULL DEFAULT '[]'::jsonb;

-- One live document of each kind per application. Regenerating replaces rather
-- than accumulating, so "the cover letter for this application" is a single row
-- and not a guess about which of four is current.
CREATE UNIQUE INDEX IF NOT EXISTS generated_documents_one_per_kind
  ON app.generated_documents (application_id, kind);

-- ---------------------------------------------------------------- grants
-- The worker drafts (it holds the API key); the renderer renders (it holds the
-- only grant on schema sensitive and has no network). Each needs what it
-- writes and nothing more.

-- Worker: drafting writes documents, answers, the questions it could not
-- answer, and the status transition that records the draft.
GRANT INSERT, UPDATE ON app.generated_documents, app.application_answers,
                        app.open_questions, app.application_status_history
  TO ja_worker;

-- Renderer: it runs its own loop, so it must be able to claim its own work.
-- Without this the render queue is claimed by nobody and silently never runs.
GRANT SELECT, INSERT, UPDATE ON app.work_items TO ja_renderer;
-- The employer's name goes on the cover letter.
GRANT SELECT ON app.companies TO ja_renderer;

-- ---------------------------------------------------------------- settings
INSERT INTO app.settings (key, value, descr) VALUES
  ('generate.min_score', '45'::jsonb,
   'Draft documents for READY groups scoring at least this. Nothing reaches the old 80.'),
  ('generate.max_drafts_per_run', '5'::jsonb,
   'Cover letters are the expensive call; this bounds a runaway batch.'),
  ('generate.artifact_dir', '"/app/artifacts"'::jsonb,
   'Renderer-local. A named volume seeded from the image so uid 10001 owns it.')
ON CONFLICT (key) DO NOTHING;
