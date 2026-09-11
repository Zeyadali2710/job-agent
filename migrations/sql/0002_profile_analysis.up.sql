-- ============================================================ profile
CREATE TABLE app.profile_versions (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  content_sha256 text        NOT NULL UNIQUE CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  snapshot       jsonb       NOT NULL,
  source_git_ref text,
  is_active      boolean     NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX profile_versions_one_active ON app.profile_versions ((true)) WHERE is_active;

CREATE TABLE app.profile_facts (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_version_id bigint  NOT NULL REFERENCES app.profile_versions(id) ON DELETE CASCADE,
  fact_id            text    NOT NULL,
  kind               text    NOT NULL,
  body               text,
  tags               text[]  NOT NULL DEFAULT '{}',
  data               jsonb   NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT profile_facts_uniq UNIQUE (profile_version_id, fact_id)
);
CREATE INDEX profile_facts_tags ON app.profile_facts USING gin (tags);

-- The one table in the `sensitive` schema. Only ja_renderer has USAGE on that schema.
CREATE TABLE sensitive.profile_sensitive (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_version_id bigint      NOT NULL REFERENCES app.profile_versions(id) ON DELETE CASCADE,
  field              text        NOT NULL CHECK (field IN (
                       'address','dob','phone','tfn','visa_grant_number','passport',
                       'referee_contacts','salary_expectations_floor','emergency_contact')),
  value              text        NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT profile_sensitive_uniq UNIQUE (profile_version_id, field)
);

-- ============================================================ analysis
CREATE TABLE app.job_analyses (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_group_id     bigint      NOT NULL REFERENCES app.job_groups(id) ON DELETE CASCADE,
  analysis_version integer     NOT NULL DEFAULT 1,
  model            text        NOT NULL,
  prompt_sha256    text,
  requirements     jsonb       NOT NULL DEFAULT '[]'::jsonb,
  skills           jsonb       NOT NULL DEFAULT '[]'::jsonb,
  work_right_clause text,
  clearance_clause  text,
  salary            jsonb       NOT NULL DEFAULT '{}'::jsonb,
  grad_metadata     jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT job_analyses_uniq UNIQUE (job_group_id, analysis_version)
);

CREATE TABLE app.job_scores (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_group_id   bigint       NOT NULL REFERENCES app.job_groups(id) ON DELETE CASCADE,
  base_score     numeric(5,2) NOT NULL CHECK (base_score BETWEEN 0 AND 100),
  llm_adjustment numeric(4,1) NOT NULL DEFAULT 0 CHECK (llm_adjustment BETWEEN -10 AND 10),
  score          numeric(5,2) NOT NULL CHECK (score BETWEEN 0 AND 100),
  band           text         NOT NULL CHECK (band IN ('HIGH','MEDIUM','LOW')),
  scorer_version text         NOT NULL,
  created_at     timestamptz  NOT NULL DEFAULT now(),
  CONSTRAINT job_scores_uniq UNIQUE (job_group_id, scorer_version)
);
CREATE INDEX job_scores_score ON app.job_scores (score DESC);

CREATE TABLE app.score_features (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  score_id     bigint       NOT NULL REFERENCES app.job_scores(id) ON DELETE CASCADE,
  feature      text         NOT NULL,
  weight       numeric(6,3) NOT NULL,
  value        numeric(8,3) NOT NULL,
  contribution numeric(8,3) NOT NULL,
  evidence     text
);
CREATE INDEX score_features_score ON app.score_features (score_id);

CREATE TABLE app.work_right_assessments (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_group_id   bigint      NOT NULL REFERENCES app.job_groups(id) ON DELETE CASCADE,
  classification text        NOT NULL CHECK (classification IN (
                   'COMPATIBLE','LIKELY_COMPATIBLE','UNCERTAIN','SPONSORSHIP_POSSIBLE','INCOMPATIBLE')),
  clause_verbatim text,
  decided_by     text        NOT NULL CHECK (decided_by IN ('RULE','LLM')),
  confidence     numeric(3,2) CHECK (confidence BETWEEN 0 AND 1),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX wra_group ON app.work_right_assessments (job_group_id);

CREATE TABLE app.clearance_assessments (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_group_id    bigint      NOT NULL REFERENCES app.job_groups(id) ON DELETE CASCADE,
  detected        text[]      NOT NULL DEFAULT '{}',
  strength        text        NOT NULL CHECK (strength IN (
                    'MANDATORY_HELD','MANDATORY_OBTAINABLE','PREFERRED','SPONSORED_AFTER_HIRE','UNCLEAR')),
  clause_verbatim text,
  decided_by      text        NOT NULL CHECK (decided_by IN ('RULE','LLM')),
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX clearance_group ON app.clearance_assessments (job_group_id);

-- Architecture §9: every outbound LLM call logged with token count and payload hash.
CREATE TABLE app.llm_calls (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purpose       text        NOT NULL,
  model         text        NOT NULL,
  payload_sha256 text       NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  tokens_in     integer     NOT NULL DEFAULT 0,
  tokens_out    integer     NOT NULL DEFAULT 0,
  cost_aud      numeric(10,6) NOT NULL DEFAULT 0,
  entity_type   text,
  entity_id     bigint,
  ok            boolean     NOT NULL,
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_created ON app.llm_calls (created_at DESC);
CREATE TRIGGER llm_calls_immutable BEFORE UPDATE OR DELETE ON app.llm_calls
  FOR EACH ROW EXECUTE FUNCTION app.deny_mutation();
