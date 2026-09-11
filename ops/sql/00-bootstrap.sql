-- Phase 3 bootstrap. Run as the superuser that owns the database.
-- Idempotent: safe to re-run.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm   SCHEMA public;
CREATE EXTENSION IF NOT EXISTS btree_gin SCHEMA public;
CREATE EXTENSION IF NOT EXISTS unaccent  SCHEMA public;

-- ---------------------------------------------------------------- roles
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[
    'ja_migrator','ja_api','ja_worker','ja_scheduler',
    'ja_notifier','ja_renderer','ja_applier','ja_readonly'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', r);
      RAISE NOTICE 'created role %', r;
    ELSE
      RAISE NOTICE 'role % already exists', r;
    END IF;
    EXECUTE format('ALTER ROLE %I SET search_path = app, public', r);
    EXECUTE format('ALTER ROLE %I SET timezone = ''UTC''', r);
    EXECUTE format('ALTER ROLE %I SET statement_timeout = ''60s''', r);
    EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = ''120s''', r);
  END LOOP;
END $$;

ALTER ROLE ja_migrator SET statement_timeout = '600s';   -- migrations may build indexes

-- ---------------------------------------------------------------- schemas
CREATE SCHEMA IF NOT EXISTS app       AUTHORIZATION ja_migrator;
CREATE SCHEMA IF NOT EXISTS sensitive AUTHORIZATION ja_migrator;

-- ---------------------------------------------------------------- baseline privileges
REVOKE ALL ON SCHEMA public    FROM PUBLIC;
REVOKE ALL ON SCHEMA sensitive FROM PUBLIC;
REVOKE ALL ON SCHEMA app       FROM PUBLIC;
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO ja_migrator, ja_api, ja_worker, ja_scheduler,
                                ja_notifier, ja_renderer, ja_applier, ja_readonly;
GRANT USAGE ON SCHEMA app    TO ja_api, ja_worker, ja_scheduler,
                                ja_notifier, ja_renderer, ja_applier, ja_readonly;

-- schema `sensitive`: exactly one role, forever.
GRANT USAGE ON SCHEMA sensitive TO ja_renderer;

GRANT CONNECT ON DATABASE :"DBNAME" TO ja_migrator, ja_api, ja_worker, ja_scheduler,
                                       ja_notifier, ja_renderer, ja_applier, ja_readonly;
