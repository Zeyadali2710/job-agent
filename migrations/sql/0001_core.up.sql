-- ============================================================ helper functions
CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION app.deny_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only: % on %.% is not permitted',
    TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'restrict_violation';
END $$;

-- ============================================================ sources
CREATE TABLE app.sources (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key                 text        NOT NULL UNIQUE,
  kind                text        NOT NULL CHECK (kind IN ('ATS','FEED','EMAIL','AGGREGATOR')),
  display_name        text        NOT NULL,
  enabled             boolean     NOT NULL DEFAULT true,
  poll_cron           text,
  rate_budget_per_day integer     CHECK (rate_budget_per_day IS NULL OR rate_budget_per_day > 0),
  automation_policy   text        NOT NULL DEFAULT 'API_ONLY'
                                  CHECK (automation_policy IN ('API_ONLY','BROWSER_ALLOWED','MANUAL_ONLY')),
  config              jsonb       NOT NULL DEFAULT '{}'::jsonb,
  circuit_state       text        NOT NULL DEFAULT 'CLOSED'
                                  CHECK (circuit_state IN ('CLOSED','OPEN','HALF_OPEN')),
  circuit_opened_at   timestamptz,
  consecutive_failures integer    NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER sources_updated BEFORE UPDATE ON app.sources
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ============================================================ companies
CREATE TABLE app.companies (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name           text        NOT NULL,
  slug           text        NOT NULL UNIQUE,
  ats_platform   text,
  ats_tenant     text,
  classification text        NOT NULL DEFAULT 'UNKNOWN'
                             CHECK (classification IN ('DIRECT_EMPLOYER','RECRUITMENT_AGENCY','UNKNOWN')),
  website        text,
  notes          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX companies_ats_tenant_key ON app.companies (ats_platform, ats_tenant)
  WHERE ats_platform IS NOT NULL AND ats_tenant IS NOT NULL;
CREATE INDEX companies_name_trgm ON app.companies USING gin (name gin_trgm_ops);
CREATE TRIGGER companies_updated BEFORE UPDATE ON app.companies
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE app.company_aliases (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id bigint      NOT NULL REFERENCES app.companies(id) ON DELETE CASCADE,
  alias      text        NOT NULL,
  alias_norm text        NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX company_aliases_company ON app.company_aliases (company_id);

CREATE TABLE app.company_rules (
  id                        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id                bigint      NOT NULL UNIQUE REFERENCES app.companies(id) ON DELETE CASCADE,
  blacklisted               boolean     NOT NULL DEFAULT false,
  blacklist_reason          text,
  max_applications_per_month integer    NOT NULL DEFAULT 2 CHECK (max_applications_per_month >= 0),
  auto_apply_allowed        boolean     NOT NULL DEFAULT false,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER company_rules_updated BEFORE UPDATE ON app.company_rules
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ============================================================ raw postings (immutable)
CREATE TABLE app.raw_postings (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id      bigint      NOT NULL REFERENCES app.sources(id),
  source_job_id  text        NOT NULL,
  url            text,
  http_status    integer,
  payload        jsonb       NOT NULL,
  payload_sha256 text        NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  fetched_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT raw_postings_dedupe UNIQUE (source_id, source_job_id, payload_sha256)
);
CREATE INDEX raw_postings_fetched ON app.raw_postings (fetched_at DESC);
CREATE TRIGGER raw_postings_immutable BEFORE UPDATE OR DELETE ON app.raw_postings
  FOR EACH ROW EXECUTE FUNCTION app.deny_mutation();
CREATE TRIGGER raw_postings_no_truncate BEFORE TRUNCATE ON app.raw_postings
  FOR EACH STATEMENT EXECUTE FUNCTION app.deny_mutation();

-- ============================================================ postings
CREATE TABLE app.postings (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  raw_id                bigint      REFERENCES app.raw_postings(id),
  source_id             bigint      NOT NULL REFERENCES app.sources(id),
  source_job_id         text        NOT NULL,
  company_id            bigint      REFERENCES app.companies(id),
  title                 text        NOT NULL,
  title_norm            text        NOT NULL,
  description           text,
  location_raw          text,
  location_class        text        NOT NULL DEFAULT 'UNKNOWN' CHECK (location_class IN (
                          'SYDNEY_CBD','SYDNEY_METRO','GREATER_SYDNEY','NSW_REGIONAL',
                          'REMOTE_AU','REMOTE_GLOBAL','HYBRID_SYDNEY','OTHER_CAPITAL',
                          'REQUIRES_RELOCATION','UNKNOWN')),
  days_in_office        smallint    CHECK (days_in_office IS NULL OR days_in_office BETWEEN 0 AND 7),
  employment_type       text        CHECK (employment_type IS NULL OR employment_type IN (
                          'FULL_TIME','PART_TIME','CONTRACT','CASUAL','INTERNSHIP','GRAD_PROGRAM')),
  seniority_class       text        CHECK (seniority_class IS NULL OR seniority_class IN (
                          'INTERN','GRADUATE','JUNIOR','MID','SENIOR','LEAD','UNKNOWN')),
  min_years_experience  smallint,
  posted_at             timestamptz,
  posted_at_confidence  text        NOT NULL DEFAULT 'UNKNOWN'
                                    CHECK (posted_at_confidence IN ('EXACT','DAY','RELATIVE','INFERRED','UNKNOWN')),
  posted_at_raw         text,
  first_seen_at         timestamptz NOT NULL DEFAULT now(),
  last_seen_at          timestamptz NOT NULL DEFAULT now(),
  closes_at             timestamptz,
  content_simhash       bigint,
  ats_platform          text,
  ats_tenant            text,
  ats_job_id            text,
  canonical_url         text,
  url_hash              text        NOT NULL CHECK (url_hash ~ '^[0-9a-f]{64}$'),
  salary                jsonb       NOT NULL DEFAULT '{}'::jsonb,
  state                 text        NOT NULL DEFAULT 'NEW' CHECK (state IN (
                          'NEW','NORMALISED','DEDUPED','SCREENED_OUT','ACTIVE','EXPIRED')),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT postings_source_key UNIQUE (source_id, source_job_id),   -- dedupe ladder L1
  CONSTRAINT postings_url_hash   UNIQUE (url_hash)                    -- dedupe ladder L3
);
-- Dedupe ladder L2 is a LOOKUP index, deliberately not unique: two sources legitimately
-- produce two posting rows for one requisition. Collapsing them is the job_group's job.
CREATE INDEX postings_ats_key ON app.postings (ats_platform, ats_tenant, ats_job_id)
  WHERE ats_platform IS NOT NULL AND ats_job_id IS NOT NULL;
CREATE INDEX postings_title_trgm  ON app.postings USING gin (title_norm gin_trgm_ops);
CREATE INDEX postings_simhash     ON app.postings (content_simhash) WHERE content_simhash IS NOT NULL;
CREATE INDEX postings_state       ON app.postings (state, first_seen_at DESC);
CREATE INDEX postings_posted_at   ON app.postings (posted_at DESC NULLS LAST);
CREATE INDEX postings_company     ON app.postings (company_id);
CREATE TRIGGER postings_updated BEFORE UPDATE ON app.postings
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE app.posting_versions (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  posting_id        bigint      NOT NULL REFERENCES app.postings(id) ON DELETE CASCADE,
  version           integer     NOT NULL CHECK (version > 0),
  content_sha256    text        NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  diff              jsonb       NOT NULL DEFAULT '{}'::jsonb,
  hard_gate_changed boolean     NOT NULL DEFAULT false,
  captured_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT posting_versions_uniq UNIQUE (posting_id, version)
);

-- ============================================================ job groups
CREATE TABLE app.job_groups (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  canonical_posting_id bigint      REFERENCES app.postings(id),
  company_id           bigint      REFERENCES app.companies(id),
  title                text        NOT NULL,
  location_class       text        NOT NULL DEFAULT 'UNKNOWN',
  status               text        NOT NULL DEFAULT 'DISCOVERED' CHECK (status IN (
                         'DISCOVERED','SCREENED_OUT','REVIEW_REQUIRED','READY','APPLIED','CLOSED')),
  screen_reason        text,
  is_grad_program      boolean     NOT NULL DEFAULT false,
  intake_year          smallint,
  application_deadline timestamptz,
  first_seen_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at         timestamptz NOT NULL DEFAULT now(),
  closes_at            timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX job_groups_status   ON app.job_groups (status, first_seen_at DESC);
CREATE INDEX job_groups_deadline ON app.job_groups (application_deadline)
  WHERE application_deadline IS NOT NULL;
CREATE TRIGGER job_groups_updated BEFORE UPDATE ON app.job_groups
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE app.job_group_postings (
  job_group_id       bigint      NOT NULL REFERENCES app.job_groups(id) ON DELETE CASCADE,
  posting_id         bigint      NOT NULL UNIQUE REFERENCES app.postings(id) ON DELETE CASCADE,
  match_layer        smallint    NOT NULL CHECK (match_layer BETWEEN 1 AND 5),
  probable_duplicate boolean     NOT NULL DEFAULT false,
  merged_at          timestamptz NOT NULL DEFAULT now(),
  merged_by          text        NOT NULL DEFAULT 'system',
  PRIMARY KEY (job_group_id, posting_id)
);

-- ============================================================ events (append-only)
CREATE TABLE app.events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  entity_type text        NOT NULL,
  entity_id   bigint,
  actor       text        NOT NULL,
  action      text        NOT NULL,
  detail      jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX events_entity ON app.events (entity_type, entity_id, at DESC);
CREATE INDEX events_at     ON app.events (at DESC);
CREATE TRIGGER events_immutable BEFORE UPDATE OR DELETE ON app.events
  FOR EACH ROW EXECUTE FUNCTION app.deny_mutation();
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON app.events
  FOR EACH STATEMENT EXECUTE FUNCTION app.deny_mutation();

-- ============================================================ queue
CREATE TABLE app.work_items (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind         text        NOT NULL,
  payload      jsonb       NOT NULL DEFAULT '{}'::jsonb,
  state        text        NOT NULL DEFAULT 'PENDING'
                           CHECK (state IN ('PENDING','RUNNING','DONE','FAILED','DEAD')),
  priority     smallint    NOT NULL DEFAULT 100,
  run_after    timestamptz NOT NULL DEFAULT now(),
  attempts     integer     NOT NULL DEFAULT 0,
  max_attempts integer     NOT NULL DEFAULT 5,
  locked_by    text,
  locked_at    timestamptz,
  last_error   text,
  dedupe_key   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT work_items_lock_coherent CHECK (
    (state = 'RUNNING' AND locked_by IS NOT NULL AND locked_at IS NOT NULL)
    OR (state <> 'RUNNING'))
);
CREATE INDEX work_items_claim ON app.work_items (state, run_after, priority)
  WHERE state = 'PENDING';
CREATE UNIQUE INDEX work_items_dedupe ON app.work_items (dedupe_key)
  WHERE dedupe_key IS NOT NULL AND state IN ('PENDING','RUNNING');
CREATE TRIGGER work_items_updated BEFORE UPDATE ON app.work_items
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ============================================================ runtime state
CREATE TABLE app.settings (
  key        text        PRIMARY KEY,
  value      jsonb       NOT NULL,
  descr      text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER settings_updated BEFORE UPDATE ON app.settings
  FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE app.heartbeats (
  worker    text        PRIMARY KEY,
  last_beat timestamptz NOT NULL DEFAULT now(),
  detail    jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- APScheduler's SQLAlchemyJobStore schema, created here so the scheduler role needs no DDL.
CREATE TABLE app.apscheduler_jobs (
  id            varchar(191)     NOT NULL PRIMARY KEY,
  next_run_time double precision,
  job_state     bytea            NOT NULL
);
CREATE INDEX apscheduler_jobs_next_run_time ON app.apscheduler_jobs (next_run_time);
