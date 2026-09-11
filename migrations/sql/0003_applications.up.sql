CREATE TABLE app.applications (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_group_id       bigint      NOT NULL REFERENCES app.job_groups(id),
  status             text        NOT NULL DEFAULT 'DRAFT' CHECK (status IN (
                       'DRAFT','READY','AWAITING_APPROVAL','APPROVED','SUBMITTING',
                       'SUBMITTED','CONFIRMED','VERIFY_REQUIRED','WITHDRAWN','FAILED_PERMANENT')),
  mode               smallint    NOT NULL DEFAULT 2 CHECK (mode BETWEEN 1 AND 3),
  profile_version_id bigint      REFERENCES app.profile_versions(id),
  submitted_at       timestamptz,
  confirmation_ref   text,
  withdrawn_reason   text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- THE constraint: at most one live application per job group. Withdrawn and permanently
-- failed applications are excluded, so a deliberate re-application is possible and an
-- accidental double application is a 23505 rather than a duplicate in someone's inbox.
CREATE UNIQUE INDEX applications_one_live_per_group
  ON app.applications (job_group_id)
  WHERE status NOT IN ('WITHDRAWN','FAILED_PERMANENT');

CREATE INDEX applications_status ON app.applications (status, updated_at DESC);
CREATE TRIGGER applications_updated BEFORE UPDATE ON app.applications
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE app.application_attempts (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id   bigint      NOT NULL REFERENCES app.applications(id) ON DELETE CASCADE,
  attempt_no       integer     NOT NULL CHECK (attempt_no > 0),
  state            text        NOT NULL DEFAULT 'PREPARED' CHECK (state IN (
                     'PREPARED','NAVIGATING','FILLING','REVIEW_PENDING','SUBMIT_INTENT',
                     'SUBMITTED','CONFIRMED','VERIFY_REQUIRED','FAILED','ABANDONED')),
  dry_run          boolean     NOT NULL,
  adapter          text,
  form_hash        text,
  submit_intent_at timestamptz,
  submitted_at     timestamptz,
  confirmation_ref text,
  trace_path       text,
  error            text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT application_attempts_uniq UNIQUE (application_id, attempt_no),
  CONSTRAINT attempt_intent_recorded CHECK (
    state NOT IN ('SUBMIT_INTENT','SUBMITTED','CONFIRMED') OR submit_intent_at IS NOT NULL)
);
CREATE TRIGGER application_attempts_updated BEFORE UPDATE ON app.application_attempts
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Third layer under DRY_RUN. The SUBMIT_INTENT row is committed BEFORE the click
-- (architecture §10), so refusing the insert prevents the click rather than merely
-- failing to record it.
CREATE OR REPLACE FUNCTION app.guard_dry_run() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE active boolean;
BEGIN
  IF NEW.state NOT IN ('SUBMIT_INTENT','SUBMITTED','CONFIRMED') THEN
    RETURN NEW;
  END IF;
  SELECT coalesce((value #>> '{}')::boolean, true) INTO active
    FROM app.settings WHERE key = 'dry_run';
  IF coalesce(active, true) AND NOT NEW.dry_run THEN
    RAISE EXCEPTION
      'DRY_RUN is active: refusing to record a live submission attempt (application_id=%)',
      NEW.application_id
      USING ERRCODE = 'restrict_violation',
            HINT = 'Set app.settings dry_run=false only as part of Phase 19 go-live.';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER application_attempts_dry_run
  BEFORE INSERT OR UPDATE ON app.application_attempts
  FOR EACH ROW EXECUTE FUNCTION app.guard_dry_run();

CREATE TABLE app.attempt_artifacts (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attempt_id   bigint      NOT NULL REFERENCES app.application_attempts(id) ON DELETE CASCADE,
  kind         text        NOT NULL CHECK (kind IN (
                 'PRE_SUBMIT_SCREENSHOT','CONFIRMATION_SCREENSHOT','TRACE','HTML_SNAPSHOT','PDF')),
  path         text        NOT NULL,
  sha256       text        CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
  bytes        bigint,
  retain_until date,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attempt_artifacts_retain ON app.attempt_artifacts (retain_until)
  WHERE retain_until IS NOT NULL;

CREATE TABLE app.generated_documents (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id     bigint      NOT NULL REFERENCES app.applications(id) ON DELETE CASCADE,
  kind               text        NOT NULL CHECK (kind IN ('RESUME','COVER_LETTER','ANSWER_SET')),
  profile_version_id bigint      NOT NULL REFERENCES app.profile_versions(id),
  path               text,
  sha256             text,
  source_ids         text[]      NOT NULL DEFAULT '{}',
  validator_state    text        NOT NULL DEFAULT 'PENDING' CHECK (validator_state IN (
                       'PENDING','PASSED','FABRICATION_SUSPECTED')),
  model              text,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX generated_documents_app ON app.generated_documents (application_id);

CREATE TABLE app.application_answers (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id bigint      NOT NULL REFERENCES app.applications(id) ON DELETE CASCADE,
  question_key   text        NOT NULL,
  question_text  text        NOT NULL,
  answer_text    text,
  can_answer     boolean     NOT NULL DEFAULT false,
  required       boolean     NOT NULL DEFAULT true,
  source_ids     text[]      NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT application_answers_uniq UNIQUE (application_id, question_key),
  CONSTRAINT answered_or_flagged CHECK (NOT can_answer OR answer_text IS NOT NULL)
);
CREATE TRIGGER application_answers_updated BEFORE UPDATE ON app.application_answers
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE app.open_questions (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id bigint      NOT NULL REFERENCES app.applications(id) ON DELETE CASCADE,
  question_text  text        NOT NULL,
  reason         text,
  resolved_at    timestamptz,
  resolved_answer text,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX open_questions_unresolved ON app.open_questions (application_id)
  WHERE resolved_at IS NULL;

CREATE TABLE app.application_status_history (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id bigint      NOT NULL REFERENCES app.applications(id) ON DELETE CASCADE,
  from_status    text,
  to_status      text        NOT NULL,
  actor          text        NOT NULL,
  reason         text,
  at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ash_app ON app.application_status_history (application_id, at DESC);
CREATE TRIGGER ash_immutable BEFORE UPDATE OR DELETE ON app.application_status_history
  FOR EACH ROW EXECUTE FUNCTION app.deny_mutation();

-- ============================================================ outbox
CREATE TABLE app.notifications (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind            text        NOT NULL CHECK (kind IN (
                    'HIGH_MATCH_JOB','APPROVAL_REQUIRED','APPLICATION_SUBMITTED',
                    'APPLICATION_FAILED','MANUAL_INTERVENTION_REQUIRED','DEADLINE_APPROACHING',
                    'SYSTEM_ERROR','SYSTEM_OFFLINE','DAILY_SUMMARY','WEEKLY_SUMMARY')),
  dedupe_key      text,
  payload         jsonb       NOT NULL,
  state           text        NOT NULL DEFAULT 'PENDING'
                              CHECK (state IN ('PENDING','SENT','DEAD')),
  attempts        integer     NOT NULL DEFAULT 0 CHECK (attempts <= 8),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  sent_at         timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX notifications_dedupe ON app.notifications (dedupe_key)
  WHERE dedupe_key IS NOT NULL;
CREATE INDEX notifications_claim ON app.notifications (state, next_attempt_at)
  WHERE state = 'PENDING';

CREATE TABLE app.application_rules (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name              text        NOT NULL UNIQUE,
  enabled           boolean     NOT NULL DEFAULT true,
  priority          smallint    NOT NULL DEFAULT 100,
  match             jsonb       NOT NULL DEFAULT '{}'::jsonb,
  action            text        NOT NULL CHECK (action IN ('AUTO_APPLY','REQUIRE_APPROVAL','SKIP')),
  company_allowlist text[]      NOT NULL DEFAULT '{}',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT auto_apply_needs_allowlist CHECK (
    action <> 'AUTO_APPLY' OR cardinality(company_allowlist) > 0)
);
CREATE TRIGGER application_rules_updated BEFORE UPDATE ON app.application_rules
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
