# PHASE 3 — Database, Migrations, Roles, and the Grants That Enforce the Design

Prerequisites: Phase 2 complete and verified. Stack running, `make health` returns
`database: ok`, snapshot `phase2-complete` exists.

Everything in this phase runs **inside CT 120 as `zeyad`**, from `~/job-agent`, unless a
block is marked **[PVE host]** — those run as `root` directly, because the host has no
`sudo`.

Take the snapshot first, **[PVE host]**:

```bash
pct snapshot 120 pre-phase3 --description "before schema, roles and migrations"
pct listsnapshot 120
```

**Do not proceed to Phase 4 until every box in §14 is ticked.**

---

## 0. Corrections to Phase 2

Six of these come from your build report; two I found while reading the guides back. Apply
all of them before starting §1 — three of them are live bugs.

### 0.1 Caddy needs a second port binding and an explicit default SNI

Both confirmed by your build. Patch `.env`:

```bash
cd ~/job-agent
grep -q '^TAILNET_IP=' .env || printf 'TAILNET_IP=100.113.104.18\n' >> .env
grep -q '^TAILNET_IP=' .env.example || printf 'TAILNET_IP=\n' >> .env.example
```

`compose.yaml`, `caddy` service:

```yaml
    ports:
      - "192.168.1.115:443:443"
      - "${TAILNET_IP}:443:443"
```

`ops/caddy/Caddyfile`, global options block:

```
{
	admin off
	auto_https disable_redirects
	default_sni 192.168.1.115
}
```

The reasoning belongs in the guide, because it will bite again the first time an IP-address
site block is added: TLS forbids IP literals in SNI, so a client connecting to
`https://192.168.1.115` sends no SNI at all. Docker's NAT rewrites the destination before
Caddy sees it, so Caddy cannot infer the site from the bind address either. With no SNI and
no default, no site block matches, no certificate is selected, and the handshake aborts with
TLS alert 80 (`internal_error`) — which looks like a certificate problem and is not one.
`default_sni` tells Caddy which site to assume when the client names none.

### 0.2 Docker starts before `tailscale0` has an address

Confirmed by your build. Keep the fix, and check it survived the last reboot:

```bash
cat /etc/sysctl.d/99-nonlocal-bind.conf     # expect: net.ipv4.ip_nonlocal_bind=1
sysctl net.ipv4.ip_nonlocal_bind            # expect: = 1
```

If the file is there but the value reads `0`, the setting was applied to the host's network
namespace rather than the container's — re-run `sysctl --system` inside CT 120.

### 0.3 `tailscale-cert.service` never reloaded Caddy

`%i` is a template-unit specifier. In a non-template unit it expands to the empty string, so
`cd /home/%i/job-agent` became `cd /home//job-agent`, failed, fell through to `cd /root`,
and `docker compose restart caddy` ran with no compose file — swallowed by `|| true`. Cert
renewal has therefore been silently ineffective since Phase 2. The corrected unit:

```bash
sudo tee /etc/systemd/system/tailscale-cert.service > /dev/null <<'EOF'
[Unit]
Description=Renew Tailscale certificate for job-agent
After=tailscaled.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c '/usr/bin/tailscale cert --cert-file /opt/job-agent/certs/cert.pem --key-file /opt/job-agent/certs/key.pem "$(/usr/bin/tailscale status --json | /usr/bin/jq -r .Self.DNSName | sed "s/\\.$//")"'
ExecStartPost=/usr/bin/docker compose -f /home/zeyad/job-agent/compose.yaml restart caddy
EOF

sudo systemctl daemon-reload
sudo systemctl start tailscale-cert.service
systemctl status tailscale-cert.service --no-pager | tail -5
```

Note the `|| true` is gone as well. A renewal that fails should be visible in
`systemctl status`, not suppressed.

### 0.4 Revised memory budget — the Chromium gate changes the table

Your gate result (attempt 1, full namespace sandbox, default seccomp, no `SYS_ADMIN`, peak
248 MiB on a real Greenhouse board and 225 MiB under a 512m cap) is the best possible
outcome and is roughly a quarter of my estimate. My 700–950 MB figure came from desktop
Chromium measurements that include a GPU process doing real compositing work; headless
Chromium on a board with no video, no WebGL and one renderer simply does not allocate that.

PHASE-02 §2 is superseded by this table:

| Service | mem_limit | cpus | Actual expected | Phase |
|---|---:|---:|---:|---|
| LXC OS + dockerd | (600 MiB) | — | ~380 MiB | 1 |
| postgres | 1280 MiB | 1.5 | ~450 MiB | 2 |
| api | 512 MiB | 1.0 | ~180 MiB | 2 |
| caddy | 128 MiB | 0.5 | ~30 MiB | 2 |
| migrator *(one-shot, `tools` profile)* | 256 MiB | 1.0 | ~90 MiB | **3** |
| worker | 1024 MiB | 2.0 | ~250 MiB | 5 |
| scheduler | 256 MiB | 0.5 | ~70 MiB | 6 |
| notifier | 192 MiB | 0.5 | ~60 MiB | 10 |
| renderer | 384 MiB | 1.0 | ~200 MiB | 12 |
| browser | **1536 MiB** | 2.0 | ~250 MiB, ~600 MiB on Workday | 15 |
| **Total long-running limits** | **5312 MiB** | | | |
| **Slack under the 8192 MiB ceiling** | **2280 MiB** | | | |

Three notes on that table. The browser drops from 3072 to 1536 MiB — still 6× your measured
peak, because Workday spawns three or four renderers where Greenhouse spawns one, and
because `shm_size` counts against the container limit. `MAX_CONCURRENT_BROWSERS: 1` stays;
the constraint was never really memory, it's that two concurrent submissions are two things
that can go wrong at the same moment. The `migrator` limit is transient — it only exists
while `alembic upgrade` runs — and `renderer` is new in this phase's design (§4).

You have reclaimed roughly 1.5 GiB. Leave it unallocated. It is the margin that lets
`pg_dump`, a vacuum and a browser run overlap without anything being killed.

### 0.5 Playwright image — carry forward to Phase 15

Recorded here so Phase 15 doesn't repeat the discovery: `mcr.microsoft.com/playwright/python`
ships the browsers and the system libraries but no importable `playwright` pip package, so
`from playwright.sync_api import ...` fails. Phase 15 builds `ops/browser/Dockerfile` from
`python:3.13-slim` with `pip install playwright && playwright install --with-deps chromium`,
and runs it as a non-root UID — your gate test ran as root, which is fine for a throwaway
probe and not fine for a container that loads arbitrary employer pages.

### 0.6 `pool_pre_ping` — fixed in §7

Correct diagnosis. Without it, SQLAlchemy hands out a pooled connection whose server side
died during a Postgres restart; asyncpg raises `ConnectionDoesNotExistError`, `/health`
returns 503, and the pool keeps handing out the same dead connections until the API is
restarted by hand. Uptime Kuma alerts, the stack looks broken, and `docker compose restart
api` "fixes" it — which is exactly the failure mode that trains you to restart things instead
of reading logs. Implemented in §7.

### 0.7 The Docker secret password can silently corrupt the DSN

Mine, not yours. `openssl rand -base64 32` draws from an alphabet that includes `+` and `/`.
A `/` inside a password breaks DSN parsing — `postgresql+asyncpg://ja_api:pa/ss@postgres:5432/jobagent`
parses `ss@postgres` as the host. Check what you generated in Phase 2:

```bash
grep -c '[+/]' /opt/job-agent/secrets/postgres_password
```

`0` means you got lucky. `1` means it contains one of those characters and your stack is
working only because asyncpg happened to tolerate the position it landed in. Either way, §7
adds `quote_plus` to the secret reader so this is structurally impossible, and §5 generates
all new role passwords from a URL-safe alphabet.

### 0.8 The vzdump exclusion list would exclude your database

Also mine. Revised architecture §7.2 says to exclude "Docker images and overlay" from
`vzdump`. Implemented the obvious way — `--exclude-path /var/lib/docker` — that also excludes
`/var/lib/docker/volumes/job-agent_pgdata`, which is the entire database. Phase 1's backup
job currently has no exclusions, so you are fine today. Record the constraint now so Phase 18
gets it right:

```
--exclude-path /var/lib/docker/overlay2
--exclude-path /var/lib/docker/tmp
--exclude-path /var/lib/docker/buildkit
# NEVER exclude /var/lib/docker/volumes
```

The `pg_dump` job in §11 is the belt to that braces. Both, not either.

---

## 1. Objective

A database that enforces the architecture's guarantees in the engine rather than in the
application code, specifically:

- the full schema from architecture §11, in three reviewable migrations
- Alembic wired to a dedicated one-shot migration service, with a tested downgrade path
- six least-privilege service roles plus a human read-only role
- `sensitive.profile_sensitive` unreachable from any role the AI code path can hold —
  enforced by a schema-level revoke, not by developer discipline
- `events` and `raw_postings` append-only against both grants and triggers
- a partial unique index that makes a double application a constraint violation
- `DRY_RUN` as a third, database-level guard under the env var and the in-function assertion
- a nightly `pg_dump`, age-encrypted, with an automated weekly restore verification

No pipeline code, no adapters, no scheduler. Phase 3 produces an empty, correct, well-defended
database and the tooling to change it safely.

---

## 2. Connection budget

`max_connections = 40` was set in Phase 2. Roles and pools are defined in this phase, so this
is where the arithmetic has to be done — running out of connections presents as intermittent
`FATAL: sorry, too many clients already` under load and is miserable to diagnose after the
fact.

| Consumer | Role | Pool | Max conns | Phase |
|---|---|---:|---:|---|
| api | `ja_api` | 5 + 5 overflow | 10 | 2 |
| worker | `ja_worker` | 4 + 4 | 8 | 5 |
| scheduler | `ja_scheduler` | 2 + 1 | 3 | 6 |
| notifier | `ja_notifier` | 2 + 1 | 3 | 10 |
| renderer | `ja_renderer` | 2 + 1 | 3 | 12 |
| applier | `ja_applier` | 2 + 2 | 4 | 14 |
| migrator (one-shot) | `ja_migrator` | 1 | 1 | 3 |
| `pg_dump` | `jobagent` | 1 | 1 | 3 |
| your `psql` | `ja_readonly` | — | 2 | 3 |
| **Total** | | | **35** | |
| `superuser_reserved_connections` (default) | | | 3 | |
| **Headroom** | | | **2** | |

That is deliberately tight, and it is the number to revisit before adding any service. If you
ever need more, raise `max_connections` and `shared_buffers` together — each connection costs
roughly 2–4 MiB of backend memory against the 1280 MiB container limit.

---

## 3. Design decisions worth understanding before you run anything

**Two schemas, not one.** `app` holds everything; `sensitive` holds exactly one table.
Architecture §16 specifies a table-level revoke; a schema-level `REVOKE USAGE` is strictly
stronger, because a role without `USAGE` on the schema cannot reach *any* object in it,
including tables added in a year's time by a migration nobody re-audited. It also fails
loudly and unambiguously — `permission denied for schema sensitive` — rather than in the
ambiguous way a missing table grant does. §12 T6 covers why that distinction matters when
you verify it.

**`bigint GENERATED ALWAYS AS IDENTITY`, not UUIDs, not `serial`.** Identity columns are the
SQL-standard replacement for `serial` and, relevant here, they do **not** require a separate
`USAGE` grant on the underlying sequence — a classic way to grant `INSERT` and still get
permission errors. UUIDs buy opacity in URLs, which is worth nothing behind Tailscale plus
basic auth, and cost you index size and locality on every foreign key.

**`text` + `CHECK`, not native enum types.** `ALTER TYPE ... ADD VALUE` cannot run in the same
transaction that uses the new value, which turns a one-line migration into a two-migration
dance. A `CHECK` constraint is dropped and recreated in one statement inside one transaction.
You give up nothing except a few bytes.

**Append-only is two mechanisms, and they guarantee different things.** Revoking
`UPDATE, DELETE, TRUNCATE` stops the service roles. It does not stop the table owner
(`ja_migrator`) or a superuser. The `BEFORE UPDATE OR DELETE` trigger stops those too — until
someone with ownership disables the trigger, which is itself a deliberate, auditable act
rather than an accident. Neither stops someone with the bootstrap password and intent. What
they do stop is a buggy `UPDATE` with a missing `WHERE`, which is the realistic threat to an
audit log.

**The unique index is partial for a reason.** `UNIQUE (job_group_id) WHERE status NOT IN
('WITHDRAWN','FAILED_PERMANENT')` means you can hold exactly one live application per job
group, and any number of dead ones. Withdraw an application and you may apply again;
`FAILED_PERMANENT` likewise. The reapply policy is then a rule the code consults, not a
constraint it can accidentally defeat.

**Migrations are SQL files executed by thin Alembic revisions.** Not `op.create_table()`.
Thirty tables of SQLAlchemy DDL is unreviewable, autogenerate would fight the models that
Phase 5 introduces, and the constraints that matter here — partial unique indexes, `CHECK`
expressions, trigger definitions — are all things you would end up writing as raw SQL anyway.
What you lose is autogenerate. What you gain is that the schema can be read in a text editor
by someone who does not know Alembic. Postgres DDL is transactional, so a revision that fails
halfway rolls back completely.

---

## 4. The role and grant model

Seven roles. All `NOSUPERUSER NOCREATEDB NOCREATEROLE NOLOGIN`-except-login, no role is a
member of another, and none owns the database.

| Role | Used by | Reads | Writes | Sensitive |
|---|---|---|---|---|
| `ja_migrator` | one-shot `migrator` service | all of `app`, `sensitive` | DDL only | owns it |
| `ja_api` | api (Phase 2) | all of `app` | applications, approvals, rules, settings, events | **no** |
| `ja_worker` | worker (Phases 5, 6, 11) | all of `app` | postings, groups, analyses, scores, work_items, events | **no** |
| `ja_scheduler` | scheduler (Phase 6) | sources, settings, heartbeats | work_items, apscheduler_jobs, heartbeats | **no** |
| `ja_notifier` | notifier (Phase 10) | notifications, groups, postings, scores | notifications only | **no** |
| `ja_renderer` | renderer (Phase 12) | profile tables, applications, documents | generated_documents, answers | **yes — only role** |
| `ja_applier` | applier (Phases 14, 15) | applications, documents, answers | attempts, artifacts, status history | **no** |
| `ja_readonly` | your `psql` | all of `app` | nothing | **no** |

The guarantee you asked for is the `ja_worker` row. The AI code path lives in the worker
container (architecture §9, Phase 11), and `ja_worker` has no `USAGE` on schema `sensitive`.
There is no connection string inside that container that can reach your TFN, visa number,
date of birth or address, so no prompt-construction bug can serialise them.

That raises the obvious question: something has to read those fields to put your address on a
PDF. That is `ja_renderer`, and the separation only means something if the container holding
that credential cannot also talk to an LLM API. **Recorded as a Phase 12 requirement:** the
`renderer` service joins the `internal` network only — no `edge`, therefore no egress, exactly
like `api` today — and does not install any LLM SDK. The container that can reach Anthropic
cannot read the sensitive schema; the container that can read the sensitive schema cannot
reach the internet. Neither restriction depends on the other being remembered.

Roles are created and granted now, in full. Their secret files are created now. Their
**mounts** are added phase by phase, as each service appears. That way the grant matrix is
audited once, here, rather than re-litigated seven times.

---

## 5. Bootstrap: roles, schemas, extensions

### 5.1 Generate the passwords

URL-safe alphabet, per §0.7:

```bash
cd ~/job-agent
for r in ja_migrator ja_api ja_worker ja_scheduler ja_notifier ja_renderer ja_applier ja_readonly; do
  f="/opt/job-agent/secrets/${r}_password"
  if [ -e "$f" ]; then echo "SKIP $f (exists)"; continue; fi
  LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 > "$f"
  chmod 600 "$f"
  echo "made $f"
done
ls -l /opt/job-agent/secrets/
```

The `if -e` guard makes this re-runnable. Re-running it without the guard would silently
rotate passwords out from under running containers, which is the sort of thing that happens
at 11pm when you are pasting blocks back into a terminal.

Verify none of them can break a DSN:

```bash
grep -lc '[^A-Za-z0-9]' /opt/job-agent/secrets/ja_*_password
```

**Expect no output.** Any filename listed means a password contains something outside the
safe alphabet.

### 5.2 The bootstrap SQL

This runs as the Phase 2 bootstrap superuser `jobagent`. It is idempotent — every statement
is guarded — so you can re-run it after a restore or when adding a role.

Note that `/docker-entrypoint-initdb.d` is **not** an option here: those scripts run only when
`initdb` creates a fresh data directory, and your `pgdata` volume already exists. We write the
file to both places anyway, so a restore onto an empty volume bootstraps itself.

```bash
mkdir -p ops/sql
cat > ops/sql/00-bootstrap.sql <<'EOF'
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
EOF
```

Apply it. `DBNAME` is passed in so the file carries no hard-coded database name:

```bash
DB=$(grep '^POSTGRES_DB=' .env | cut -d= -f2)
SU=$(grep '^POSTGRES_USER=' .env | cut -d= -f2)

docker compose exec -T postgres sh -c \
  "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB \
   -v DBNAME=$DB -v ON_ERROR_STOP=1" < ops/sql/00-bootstrap.sql
```

**Expect** a run of `NOTICE: created role ja_...` lines and `GRANT`/`REVOKE` acknowledgements,
with no `ERROR`. Re-run it immediately — the second run should say `role ... already exists`
eight times and otherwise look identical. If the second run errors, the file is not idempotent
and something above was edited.

Copy it where a future fresh volume will find it, and wire it into compose:

```bash
sudo mkdir -p /opt/job-agent/initdb
sudo cp ops/sql/00-bootstrap.sql /opt/job-agent/initdb/
sudo sed -i 's/:"DBNAME"/CURRENT_DATABASE()/' /opt/job-agent/initdb/00-bootstrap.sql
```

That `sed` is needed because `initdb` scripts run without `-v`. `REVOKE ... ON DATABASE
CURRENT_DATABASE()` is not valid syntax, so open the copied file and replace those two lines
with the literal database name — this is one of the few places a hard-coded value is simpler
than the alternative:

```bash
sudo sed -i "s/CURRENT_DATABASE()/$DB/g" /opt/job-agent/initdb/00-bootstrap.sql
sudo grep -n 'DATABASE' /opt/job-agent/initdb/00-bootstrap.sql
```

Add the mount to `compose.yaml` under `postgres`:

```yaml
      - /opt/job-agent/initdb:/docker-entrypoint-initdb.d:ro
```

This mount does nothing today. It is there so that a restore onto an empty volume produces a
database with roles and schemas already present, instead of a stack that comes up healthy and
then fails every migration.

---

## 6. Alembic

### 6.1 Dependencies

```bash
cat >> requirements.txt <<'EOF'
alembic==1.14.0
psycopg[binary]==3.2.3
EOF
tail -4 requirements.txt
```

`psycopg` is the sync driver, used by Alembic only. Migrations that hang are hard enough to
debug without an event loop in the picture, and the ~8 MiB in the image is a fair trade.
`asyncpg` remains the only driver the running application uses.

### 6.2 Configuration

```bash
cat > alembic.ini <<'EOF'
[alembic]
script_location = migrations
prepend_sys_path = src
timezone = UTC
version_path_separator = os

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
EOF
```

Note there is no `sqlalchemy.url` line. The URL comes from `Settings`, so the credentials live
in exactly one place and Alembic cannot be pointed at the wrong database by an edit to an ini
file.

```bash
mkdir -p migrations/versions migrations/sql

cat > migrations/env.py <<'EOF'
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from job_agent.core.config import get_settings

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

SQL_DIR = Path(__file__).parent / "sql"


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table="alembic_version",
            version_table_schema="app",
            include_schemas=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Offline mode is not supported; migrations need a live connection.")
run_migrations_online()
EOF

cat > migrations/script.py.mako <<'EOF'
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from pathlib import Path

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL / "${up_revision}.up.sql").read_text())


def downgrade() -> None:
    op.execute((SQL / "${up_revision}.down.sql").read_text())
EOF
```

`transaction_per_migration=True` means each revision commits independently. Applying three
revisions and having the third fail leaves the first two applied and recorded, rather than
rolling back all three — which is what you want, because the alternative makes a partially
successful upgrade indistinguishable from a failed one.

### 6.3 The migrator service

Add to `compose.yaml`, and add the new secrets to the top-level `secrets:` block:

```yaml
secrets:
  postgres_password:
    file: /opt/job-agent/secrets/postgres_password
  ja_migrator_password:
    file: /opt/job-agent/secrets/ja_migrator_password
  ja_api_password:
    file: /opt/job-agent/secrets/ja_api_password
  discord_webhook:
    file: /opt/job-agent/secrets/discord_webhook
  anthropic_api_key:
    file: /opt/job-agent/secrets/anthropic_api_key
```

```yaml
  migrator:
    build: .
    profiles: ["tools"]
    restart: "no"
    mem_limit: 256m
    memswap_limit: 256m
    cpus: 1.0
    pids_limit: 128
    init: true
    read_only: true
    tmpfs:
      - /tmp:size=32m
    security_opt:
      - no-new-privileges:true
    cap_drop: [ALL]
    environment:
      POSTGRES_USER: ja_migrator
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_HOST: postgres
      TZ: UTC
    secrets:
      - ja_migrator_password
    volumes:
      - ./migrations:/app/migrations:ro
      - ./alembic.ini:/app/alembic.ini:ro
    depends_on:
      postgres:
        condition: service_healthy
    command: ["alembic", "upgrade", "head"]
    logging: *default-logging
    networks: [internal]
```

`profiles: ["tools"]` keeps it out of `docker compose up`. It runs only when named explicitly,
which is what "used only by the migration job" in architecture §8 means in practice.

### 6.4 Makefile

```bash
cat >> Makefile <<'EOF'

.PHONY: migrate migrate-down migrate-status revision bootstrap grants seed backup restore-test psql-ro
bootstrap:      ; @DB=$$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$$(grep '^POSTGRES_USER=' .env | cut -d= -f2); \
                  docker compose exec -T postgres sh -c "PGPASSWORD=\$$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $$SU -d $$DB -v DBNAME=$$DB -v ON_ERROR_STOP=1" < ops/sql/00-bootstrap.sql
grants:         ; @DB=$$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$$(grep '^POSTGRES_USER=' .env | cut -d= -f2); \
                  docker compose exec -T postgres sh -c "PGPASSWORD=\$$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $$SU -d $$DB -v ON_ERROR_STOP=1" < ops/sql/10-grants.sql
seed:           ; @DB=$$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$$(grep '^POSTGRES_USER=' .env | cut -d= -f2); \
                  docker compose exec -T postgres sh -c "PGPASSWORD=\$$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $$SU -d $$DB -v ON_ERROR_STOP=1" < ops/sql/20-seed.sql
migrate:        ; docker compose run --rm migrator alembic upgrade head
migrate-down:   ; docker compose run --rm migrator alembic downgrade -1
migrate-status: ; docker compose run --rm migrator alembic current -v
revision:       ; docker compose run --rm migrator alembic revision -m "$(m)"
psql-ro:        ; docker compose exec -e PGPASSWORD=$$(cat /opt/job-agent/secrets/ja_readonly_password) postgres psql -h 127.0.0.1 -U ja_readonly -d $$(grep '^POSTGRES_DB=' .env | cut -d= -f2)
backup:         ; sudo /opt/job-agent/bin/pg-backup.sh
restore-test:   ; sudo /opt/job-agent/bin/pg-restore-test.sh
EOF
```

`make psql-ro` reads a secret from the host filesystem and passes it as an environment
variable into an `exec` — acceptable for an interactive session you are watching, and the
reason it is `ja_readonly` and not a write-capable role.

---

## 7. Application configuration

Three changes: percent-encode the password, name the role per service, and fix the pool.

### 7.1 `src/job_agent/core/config.py`

```python
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(name: str) -> str:
    """Read a Docker secret file. Fails loudly rather than falling back to env."""
    path = Path("/run/secrets") / name
    if not path.exists():
        raise RuntimeError(f"Required secret '{name}' not mounted at {path}")
    value = path.read_text().strip()
    if not value:
        raise RuntimeError(f"Secret '{name}' at {path} is empty")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    dry_run: bool = True
    log_level: str = "INFO"
    tz: str = "Australia/Sydney"

    postgres_user: str = "ja_api"
    postgres_db: str = "jobagent"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    @property
    def _password(self) -> str:
        # Percent-encoded: '/' and '+' in a password otherwise corrupt the DSN.
        return quote_plus(_read_secret(f"{self.postgres_user}_password"))

    def _dsn(self, driver: str) -> str:
        return (
            f"postgresql+{driver}://{self.postgres_user}:{self._password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url(self) -> str:
        """Async DSN — asyncpg. Used by every long-running service."""
        return self._dsn("asyncpg")

    @property
    def sync_database_url(self) -> str:
        """Sync DSN — psycopg. Used by Alembic only."""
        return self._dsn("psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

The secret filename is derived from the role name, so a service that declares
`POSTGRES_USER: ja_worker` and forgets to mount `ja_worker_password` fails at startup with a
message naming the missing file. That is the same fail-loudly property Phase 2 established,
extended to the role split.

### 7.2 `src/job_agent/web/app.py` — the engine

Replace the `lifespan` function and add the schema version to `/health`:

```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from job_agent.core.config import get_settings

settings = get_settings()
engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = create_async_engine(
        settings.database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,      # validate a pooled connection before handing it out
        pool_recycle=1800,       # never reuse a connection older than 30 minutes
        pool_timeout=10,
        connect_args={
            "timeout": 5,
            "server_settings": {
                "application_name": "job-agent-api",
                "timezone": "UTC",
            },
        },
    )
    yield
    await engine.dispose()


app = FastAPI(title="Job Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    checks = {
        "api": "ok",
        "database": "unknown",
        "dry_run": settings.dry_run,
        "role": None,
        "schema_version": None,
    }
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as conn:
                checks["role"] = (await conn.execute(text("SELECT current_user"))).scalar()
                checks["schema_version"] = (
                    await conn.execute(text("SELECT version_num FROM app.alembic_version"))
                ).scalar()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        return JSONResponse(checks, status_code=503)
    return checks
```

`pool_pre_ping` costs one round trip per checkout — on a unix-adjacent Docker bridge that is
tens of microseconds, against a failure mode that requires manual intervention. `pool_recycle`
covers the slower version of the same problem, where an idle connection is reaped by a
firewall or `idle_session_timeout` rather than by a restart.

Surfacing `role` and `schema_version` on `/health` means "is the API connecting as the role I
think, against the schema I think" is one curl rather than an investigation.

### 7.3 `compose.yaml` — move the api off the superuser

Under `api`:

```yaml
    environment:
      DRY_RUN: ${DRY_RUN}
      LOG_LEVEL: ${LOG_LEVEL}
      TZ: ${TZ}
      POSTGRES_USER: ja_api          # was ${POSTGRES_USER} — the bootstrap superuser
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_HOST: postgres
    secrets:
      - ja_api_password              # was postgres_password
```

`POSTGRES_USER` in `.env` stays as `jobagent` — it is still what `initdb` uses and what the
healthcheck's `pg_isready` and the backup script authenticate as. It is simply no longer what
the application connects as. Leave a comment in `.env` saying so, because a variable named
`POSTGRES_USER` that is not the user any service uses is otherwise a trap for future you:

```bash
sed -i 's/^POSTGRES_USER=jobagent/# bootstrap superuser: initdb, backups, grants only. Services use ja_* roles.\nPOSTGRES_USER=jobagent/' .env
```

Rebuild and restart, but **do not** expect the API to be healthy yet — the grants in §9 have
not run:

```bash
make build
docker compose up -d api
docker compose logs --tail=20 api
```

---

## 8. The schema

Three revisions. Create the SQL first, then the revision files.

### 8.1 Revision 0001 — core

```bash
cat > migrations/sql/0001_core.up.sql <<'EOF'
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
EOF

cat > migrations/sql/0001_core.down.sql <<'EOF'
DROP TABLE IF EXISTS app.apscheduler_jobs, app.heartbeats, app.settings,
  app.work_items, app.events, app.job_group_postings, app.job_groups,
  app.posting_versions, app.postings, app.raw_postings, app.company_rules,
  app.company_aliases, app.companies, app.sources CASCADE;
DROP FUNCTION IF EXISTS app.deny_mutation();
DROP FUNCTION IF EXISTS app.set_updated_at();
EOF
```

### 8.2 Revision 0002 — profile and analysis

```bash
cat > migrations/sql/0002_profile_analysis.up.sql <<'EOF'
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
EOF

cat > migrations/sql/0002_profile_analysis.down.sql <<'EOF'
DROP TABLE IF EXISTS app.llm_calls, app.clearance_assessments, app.work_right_assessments,
  app.score_features, app.job_scores, app.job_analyses CASCADE;
DROP TABLE IF EXISTS sensitive.profile_sensitive CASCADE;
DROP TABLE IF EXISTS app.profile_facts, app.profile_versions CASCADE;
EOF
```

### 8.3 Revision 0003 — applications

```bash
cat > migrations/sql/0003_applications.up.sql <<'EOF'
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
EOF

cat > migrations/sql/0003_applications.down.sql <<'EOF'
DROP TABLE IF EXISTS app.application_rules, app.notifications,
  app.application_status_history, app.open_questions, app.application_answers,
  app.generated_documents, app.attempt_artifacts, app.application_attempts,
  app.applications CASCADE;
DROP FUNCTION IF EXISTS app.guard_dry_run();
EOF
```

`auto_apply_needs_allowlist` is worth pausing on. Architecture §0.3 says Mode 3 is
per-company allowlisted and off by default. That `CHECK` means a rule with `action =
'AUTO_APPLY'` and an empty allowlist cannot exist as a row. The blanket auto-apply rule is not
discouraged by config; it is unrepresentable.

### 8.4 Create the revisions

```bash
docker compose build migrator
docker compose run --rm migrator alembic revision -m "core" --rev-id 0001_core
docker compose run --rm migrator alembic revision -m "profile and analysis" --rev-id 0002_profile_analysis
docker compose run --rm migrator alembic revision -m "applications" --rev-id 0003_applications
ls -1 migrations/versions/
```

**Expect** three files named `0001_core_core.py`, `0002_profile_analysis_*.py`,
`0003_applications_*.py`. Confirm the chain before applying anything:

```bash
grep -H -E '^(revision|down_revision)' migrations/versions/*.py
```

**Expect** `0001` with `down_revision = None`, `0002` pointing at `0001`, `0003` at `0002`.
If any `down_revision` is `None` other than 0001's, Alembic will treat them as separate heads
and `upgrade head` will fail with `Multiple head revisions are present`.

The files are created by the container as root. Fix ownership so you can edit them:

```bash
sudo chown -R zeyad:zeyad migrations/versions
```

---

## 9. Apply, then grant

### 9.1 Apply

```bash
make migrate
make migrate-status
```

**Expect** three `Running upgrade` lines and `Rev: 0003_applications (head)`.

### 9.2 Grants

Grants run **after** the migrations, because the objects have to exist. `ALTER DEFAULT
PRIVILEGES` then covers objects created by future migrations, so this file is applied once
per phase that adds tables — not once per table.

```bash
cat > ops/sql/10-grants.sql <<'EOF'
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- broad read
GRANT SELECT ON ALL TABLES IN SCHEMA app TO ja_api, ja_worker, ja_readonly;

ALTER DEFAULT PRIVILEGES FOR ROLE ja_migrator IN SCHEMA app
  GRANT SELECT ON TABLES TO ja_api, ja_worker, ja_readonly;

-- ---------------------------------------------------------------- api
GRANT INSERT, UPDATE ON
  app.applications, app.application_answers, app.open_questions,
  app.application_status_history, app.application_rules, app.settings,
  app.company_rules, app.job_groups, app.notifications
  TO ja_api;
GRANT INSERT ON app.events TO ja_api;

-- ---------------------------------------------------------------- worker
GRANT INSERT, UPDATE ON
  app.postings, app.posting_versions, app.job_groups, app.job_group_postings,
  app.companies, app.company_aliases, app.job_analyses, app.job_scores,
  app.score_features, app.work_right_assessments, app.clearance_assessments,
  app.work_items, app.heartbeats, app.applications, app.sources,
  app.profile_versions, app.profile_facts
  TO ja_worker;
GRANT INSERT ON app.raw_postings, app.events, app.llm_calls, app.notifications TO ja_worker;
GRANT DELETE ON app.job_group_postings TO ja_worker;   -- unmerge, dashboard-initiated

-- ---------------------------------------------------------------- scheduler
GRANT SELECT ON app.sources, app.settings, app.job_groups TO ja_scheduler;
GRANT INSERT, UPDATE, DELETE ON app.apscheduler_jobs TO ja_scheduler;
GRANT INSERT, UPDATE ON app.work_items, app.heartbeats TO ja_scheduler;
GRANT INSERT ON app.events TO ja_scheduler;

-- ---------------------------------------------------------------- notifier
-- Architecture §8: "Notifier has no profile read." Explicit list, no ALL TABLES.
GRANT SELECT ON app.notifications, app.job_groups, app.postings, app.companies,
                app.job_scores, app.score_features, app.settings TO ja_notifier;
GRANT UPDATE ON app.notifications TO ja_notifier;
GRANT INSERT ON app.events TO ja_notifier;
GRANT UPDATE, INSERT ON app.heartbeats TO ja_notifier;

-- ---------------------------------------------------------------- renderer (Phase 12)
GRANT SELECT ON app.applications, app.job_groups, app.postings, app.job_analyses,
                app.profile_versions, app.profile_facts, app.application_answers,
                app.generated_documents, app.settings TO ja_renderer;
GRANT INSERT, UPDATE ON app.generated_documents, app.application_answers TO ja_renderer;
GRANT INSERT ON app.events, app.open_questions TO ja_renderer;
GRANT UPDATE, INSERT ON app.heartbeats TO ja_renderer;

-- The only sensitive grant in the system.
GRANT SELECT ON sensitive.profile_sensitive TO ja_renderer;
ALTER DEFAULT PRIVILEGES FOR ROLE ja_migrator IN SCHEMA sensitive
  GRANT SELECT ON TABLES TO ja_renderer;

-- ---------------------------------------------------------------- applier (Phases 14-15)
GRANT SELECT ON app.applications, app.application_attempts, app.attempt_artifacts,
                app.generated_documents, app.application_answers, app.open_questions,
                app.job_groups, app.postings, app.companies, app.company_rules,
                app.application_rules, app.settings, app.work_items TO ja_applier;
GRANT INSERT, UPDATE ON app.application_attempts, app.attempt_artifacts TO ja_applier;
GRANT UPDATE ON app.applications TO ja_applier;
GRANT INSERT ON app.application_status_history, app.events, app.notifications TO ja_applier;
GRANT UPDATE, INSERT ON app.work_items, app.heartbeats TO ja_applier;

-- ---------------------------------------------------------------- hard denials
-- Belt and braces over the append-only triggers, and over anything ALL TABLES gave away.
REVOKE UPDATE, DELETE, TRUNCATE ON app.events, app.raw_postings, app.llm_calls,
       app.application_status_history
  FROM ja_api, ja_worker, ja_scheduler, ja_notifier, ja_renderer, ja_applier, ja_readonly;

-- No service role may ever reach the sensitive schema except the renderer.
REVOKE ALL ON SCHEMA sensitive
  FROM ja_api, ja_worker, ja_scheduler, ja_notifier, ja_applier, ja_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA sensitive
  FROM ja_api, ja_worker, ja_scheduler, ja_notifier, ja_applier, ja_readonly;

-- readonly is exactly that.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA app FROM ja_readonly;
EOF

make grants
```

**Expect** a column of `GRANT`, `REVOKE` and `ALTER DEFAULT PRIVILEGES` with no `ERROR`.

Note there are no `GRANT USAGE ... ON SEQUENCES` statements. Identity columns do not need
them — a point worth remembering the first time a `serial` column appears in a later phase and
`INSERT` fails with `permission denied for sequence`.

Restart the API onto its new role:

```bash
docker compose up -d --force-recreate api
sleep 10 && make health
```

**Expect** `"role": "ja_api"` and `"schema_version": "0003_applications"`.

---

## 10. Seed data

Minimal and honest. The ten source rows are facts from architecture §5. The five companies are
placeholders with `ats_tenant` deliberately `NULL` — Phase 5 fills the real registry by
inspecting each careers page's network tab, and seeding guessed tenant tokens now would
produce a registry that looks authoritative and 404s.

```bash
cat > ops/sql/20-seed.sql <<'EOF'
\set ON_ERROR_STOP on
BEGIN;

INSERT INTO app.sources (key, kind, display_name, poll_cron, rate_budget_per_day, automation_policy) VALUES
  ('greenhouse',     'ATS',        'Greenhouse',      '*/30 * * * *', NULL, 'API_ONLY'),
  ('lever',          'ATS',        'Lever',           '7,37 * * * *', NULL, 'API_ONLY'),
  ('ashby',          'ATS',        'Ashby',           '*/30 * * * *', NULL, 'API_ONLY'),
  ('workable',       'ATS',        'Workable',        '*/30 * * * *', NULL, 'API_ONLY'),
  ('smartrecruiters','ATS',        'SmartRecruiters', '*/30 * * * *', NULL, 'API_ONLY'),
  ('workday',        'ATS',        'Workday',         '*/30 * * * *', NULL, 'API_ONLY'),
  ('pageup',         'ATS',        'PageUp',          '*/30 * * * *', NULL, 'API_ONLY'),
  ('personio',       'ATS',        'Personio',        '*/30 * * * *', NULL, 'API_ONLY'),
  ('adzuna',         'AGGREGATOR', 'Adzuna AU',       '13 */6 * * *',   30, 'API_ONLY'),
  ('imap',           'EMAIL',      'Email alerts',    '*/15 * * * *', NULL, 'MANUAL_ONLY')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app.settings (key, value, descr) VALUES
  ('dry_run',                  'true'::jsonb, 'Third-layer submit guard. False only at Phase 19.'),
  ('daily_application_cap',    '8'::jsonb,    'Global cap, architecture §13'),
  ('freshness_window_hours',   '24'::jsonb,   'Scoring input, never a hard gate (§0.2)'),
  ('reapply_policy',           '"never"'::jsonb, 'never | after_days'),
  ('ai_monthly_spend_cap_aud', '25'::jsonb,   'Halts enrichment when exceeded'),
  ('notify_min_score',         '80'::jsonb,   'HIGH_MATCH_JOB threshold')
ON CONFLICT (key) DO NOTHING;

INSERT INTO app.application_rules (name, enabled, priority, match, action) VALUES
  ('default-require-approval', true, 1000, '{}'::jsonb, 'REQUIRE_APPROVAL')
ON CONFLICT (name) DO NOTHING;

INSERT INTO app.companies (name, slug, classification, website) VALUES
  ('Canva',          'canva',          'DIRECT_EMPLOYER', 'https://www.canva.com/careers/'),
  ('Atlassian',      'atlassian',      'DIRECT_EMPLOYER', 'https://www.atlassian.com/company/careers'),
  ('SafetyCulture',  'safetyculture',  'DIRECT_EMPLOYER', 'https://safetyculture.com/careers/'),
  ('Airwallex',      'airwallex',      'DIRECT_EMPLOYER', 'https://www.airwallex.com/careers'),
  ('WiseTech Global','wisetech-global','DIRECT_EMPLOYER', 'https://www.wisetechglobal.com/careers/')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO app.events (entity_type, entity_id, actor, action, detail)
VALUES ('system', NULL, 'phase-3', 'schema_seeded',
        jsonb_build_object('revision', (SELECT version_num FROM app.alembic_version)));

COMMIT;
EOF

make seed
```

Every insert is `ON CONFLICT DO NOTHING` except the event, which is append-only by design and
should record each run.

---

## 11. Backup

### 11.1 The age key — read this before running it

This is the irreversible step in Phase 3. **If you lose the private key, every encrypted
backup you have is permanently unreadable.** No recovery, no support channel, no brute force.

```bash
sudo apt install -y age
sudo mkdir -p /opt/job-agent/backups/{daily,weekly} /opt/job-agent/bin
sudo chmod 700 /opt/job-agent/backups

sudo age-keygen -o /opt/job-agent/secrets/backup-age.key 2>/dev/null
sudo chmod 600 /opt/job-agent/secrets/backup-age.key
sudo grep 'public key' /opt/job-agent/secrets/backup-age.key
sudo age-keygen -y /opt/job-agent/secrets/backup-age.key | sudo tee /opt/job-agent/secrets/backup-age.pub
```

**Now, before anything depends on it:** print the private key and put it in your password
manager, under an entry named so that future-you at 2am can find it.

```bash
sudo cat /opt/job-agent/secrets/backup-age.key
```

The private key stays on the box as well, at 600 root, so the weekly restore verification can
run unattended. Be clear about what that buys and what it doesn't: it protects the copies you
carry off-box on the second NVMe, and it protects an archive that ends up somewhere it
shouldn't. It does not protect against someone who already has root in CT 120 — they have the
key and the live database anyway. If you would rather not have the key on disk, delete it here
and supply it interactively for restore tests; the nightly dump only needs the public key.

### 11.2 The dump script

```bash
sudo tee /opt/job-agent/bin/pg-backup.sh > /dev/null <<'EOF'
#!/bin/bash
set -euo pipefail

REPO=/home/zeyad/job-agent
DEST=/opt/job-agent/backups
RECIP=$(cat /opt/job-agent/secrets/backup-age.pub)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DOW=$(date +%u)                       # 7 = Sunday
OUT="$DEST/daily/jobagent-$STAMP.dump.age"

cd "$REPO"
DB=$(grep '^POSTGRES_DB=' .env | cut -d= -f2)
SU=$(grep '^POSTGRES_USER=' .env | cut -d= -f2)

umask 077
docker compose exec -T postgres sh -c \
  "PGPASSWORD=\$(cat /run/secrets/postgres_password) pg_dump -h 127.0.0.1 -U $SU -d $DB -Fc --no-password" \
  | age -r "$RECIP" -o "$OUT"

SIZE=$(stat -c %s "$OUT")
if [ "$SIZE" -lt 4096 ]; then
  echo "FAIL: dump is only ${SIZE} bytes" >&2
  rm -f "$OUT"; exit 1
fi

# Prove the archive is decryptable and structurally valid before trusting it.
age -d -i /opt/job-agent/secrets/backup-age.key "$OUT" \
  | docker compose exec -T postgres pg_restore --list > /dev/null
echo "verified: $OUT ($SIZE bytes)"

[ "$DOW" = "7" ] && cp "$OUT" "$DEST/weekly/"

find "$DEST/daily"  -name '*.dump.age' -mtime +14 -delete
find "$DEST/weekly" -name '*.dump.age' -mtime +56 -delete
ls -1 "$DEST/daily" | wc -l | xargs printf 'daily archives: %s\n'
EOF
sudo chmod 700 /opt/job-agent/bin/pg-backup.sh
```

The `pg_restore --list` step is what separates a backup from a file. A dump that fails midway
still leaves bytes on disk; without the listing check you would find out on the night you
needed it.

### 11.3 Restore verification

```bash
sudo tee /opt/job-agent/bin/pg-restore-test.sh > /dev/null <<'EOF'
#!/bin/bash
set -euo pipefail

REPO=/home/zeyad/job-agent
cd "$REPO"
DB=$(grep '^POSTGRES_DB=' .env | cut -d= -f2)
SU=$(grep '^POSTGRES_USER=' .env | cut -d= -f2)
SCRATCH=jobagent_restoretest
LATEST=$(ls -1t /opt/job-agent/backups/daily/*.dump.age | head -1)

echo "restoring $LATEST into $SCRATCH"
psql_su() { docker compose exec -T postgres sh -c \
  "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d ${1:-postgres} -v ON_ERROR_STOP=1 -tAc \"$2\""; }

psql_su postgres "DROP DATABASE IF EXISTS $SCRATCH"
psql_su postgres "CREATE DATABASE $SCRATCH"

age -d -i /opt/job-agent/secrets/backup-age.key "$LATEST" \
  | docker compose exec -T postgres sh -c \
    "PGPASSWORD=\$(cat /run/secrets/postgres_password) pg_restore -h 127.0.0.1 -U $SU -d $SCRATCH --no-owner --no-privileges" \
  || echo "note: pg_restore reported non-fatal warnings (roles/ownership)"

LIVE=$(psql_su "$DB"      "SELECT count(*) FROM app.sources")
REST=$(psql_su "$SCRATCH" "SELECT count(*) FROM app.sources")
echo "app.sources  live=$LIVE  restored=$REST"
[ "$LIVE" = "$REST" ] || { echo "MISMATCH" >&2; exit 1; }

psql_su postgres "DROP DATABASE $SCRATCH"
echo "restore verified"
EOF
sudo chmod 700 /opt/job-agent/bin/pg-restore-test.sh
```

`--no-owner --no-privileges` is correct here: the scratch database has no `ja_*` grants, and
the point of the test is that the *data* restores, not that the ACLs do. Grants are recreated
by `ops/sql/00-bootstrap.sql` and `10-grants.sql` during a real recovery, which is why those
files are in git.

### 11.4 Timer

```bash
sudo tee /etc/systemd/system/pg-backup.service > /dev/null <<'EOF'
[Unit]
Description=Encrypted pg_dump of the job-agent database
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/opt/job-agent/bin/pg-backup.sh
EOF

sudo tee /etc/systemd/system/pg-backup.timer > /dev/null <<'EOF'
[Unit]
Description=Nightly job-agent database backup

[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo tee /etc/systemd/system/pg-restore-test.service > /dev/null <<'EOF'
[Unit]
Description=Weekly restore verification of the latest job-agent backup
After=docker.service

[Service]
Type=oneshot
ExecStart=/opt/job-agent/bin/pg-restore-test.sh
EOF

sudo tee /etc/systemd/system/pg-restore-test.timer > /dev/null <<'EOF'
[Unit]
Description=Weekly restore verification

[Timer]
OnCalendar=Sun *-*-* 04:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pg-backup.timer pg-restore-test.timer
systemctl list-timers 'pg-*' --no-pager
```

One timing caveat, because it will confuse you exactly once: the container's timezone is
`Australia/Sydney`, so `02:30` is local. On the first Sunday in October the local hour
02:00–03:00 does not exist, and systemd fires the unit at the next valid time instead. In
April that hour occurs twice and `OnCalendar` fires once. Neither breaks anything here — the
job is idempotent and `Persistent=true` catches missed runs — but if you ever want a strictly
invariant 24-hour interval, change the line to `OnCalendar=*-*-* 16:30:00 UTC`.

The 02:30 slot deliberately sits before the 03:15 `vzdump`, so each night's archive contains
that night's dump.

---

## 12. Verification

Run these in order. Each one says what right looks like **and what wrong looks like**, because
several of these tests can pass for the wrong reason.

### T1 — Roles exist with no dangerous attributes

```bash
make psql-ro
```
```sql
\du ja_*
```

**Right:** eight roles, `Attributes` column empty or showing only `Password valid until`.
**Wrong:** any `Superuser`, `Create role`, `Create DB` or `Bypass RLS`. A `ja_*` role with
`Superuser` makes every grant in §9 decorative — the schema-level revoke on `sensitive` is
ignored for superusers. Fix by re-running §5.2, which sets `NOSUPERUSER` explicitly.

### T2 — Extensions and schemas

```sql
SELECT extname FROM pg_extension ORDER BY 1;
SELECT nspname, pg_get_userbyid(nspowner) AS owner
  FROM pg_namespace WHERE nspname IN ('app','sensitive','public');
```

**Right:** `btree_gin`, `pg_trgm`, `plpgsql`, `unaccent`; `app` and `sensitive` owned by
`ja_migrator`. **Wrong:** `app` owned by `jobagent` — the migrator will then fail to create
tables, or worse, will succeed because it inherited something it shouldn't have.

### T3 — Migration chain

```bash
make migrate-status
```

**Right:** `Rev: 0003_applications (head)`, three revisions listed in order.
**Wrong:** `Multiple head revisions are present` (a `down_revision` is wrong — see §8.4), or
`Can't locate revision identified by ...` (a versions file was deleted after being applied;
recover by re-creating the file, not by editing `app.alembic_version`).

### T4 — The downgrade path works

Do this now, while the database is empty. A downgrade you have never run is not a rollback
plan.

```bash
make migrate-down && make migrate-down && make migrate-down
docker compose run --rm migrator alembic current
make migrate
make migrate-status
```

**Right:** `alembic current` prints nothing between the downgrades and the re-upgrade, then
`0003_applications (head)` at the end. **Wrong:** any `DependentObjectsStillExist` or
`cannot drop ... because other objects depend on it` — a `.down.sql` is missing a `CASCADE` or
dropping in the wrong order. Fix the down file now; discovering it during a real rollback is
the worst possible time.

Re-seed, since the downgrade dropped the tables:

```bash
make grants && make seed
```

### T5 — Object inventory

```sql
SELECT table_schema, count(*)
  FROM information_schema.tables
 WHERE table_schema IN ('app','sensitive') AND table_type='BASE TABLE'
 GROUP BY 1;
```

**Right:** `app` = 32 (31 tables plus `alembic_version`), `sensitive` = 1.
**Wrong:** `sensitive` = 0 means `profile_sensitive` landed in `app`, and every test in T6
will pass for entirely the wrong reason.

### T6 — The grant that matters

This is the one you asked for, and it is the test most likely to give a false pass. Establish
first that the table exists and has a row, **as the superuser**:

```bash
DB=$(grep '^POSTGRES_DB=' .env | cut -d= -f2); SU=$(grep '^POSTGRES_USER=' .env | cut -d= -f2)
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB" <<'SQL'
INSERT INTO app.profile_versions (content_sha256, snapshot)
VALUES (repeat('a',64), '{"test":true}') ON CONFLICT DO NOTHING;
INSERT INTO sensitive.profile_sensitive (profile_version_id, field, value)
SELECT id, 'tfn', 'TEST-VALUE-DELETE-ME' FROM app.profile_versions WHERE content_sha256 = repeat('a',64)
ON CONFLICT DO NOTHING;
SELECT count(*) AS sensitive_rows FROM sensitive.profile_sensitive;
SQL
```

**Expect `sensitive_rows = 1`.** Now try to read it as the role the AI code path will hold:

```bash
docker compose exec -e PGPASSWORD=$(cat /opt/job-agent/secrets/ja_worker_password) -T postgres \
  psql -h 127.0.0.1 -U ja_worker -d $DB -c 'SELECT * FROM sensitive.profile_sensitive;'
```

**Right:** `ERROR: permission denied for schema sensitive`.
**Wrong, and the important one:** `ERROR: relation "sensitive.profile_sensitive" does not
exist`. That is not a pass. It means the table isn't there, so you have proven nothing about
grants — go back to T5. Also wrong, obviously: a result set.

Repeat for every role that must not see it:

```bash
for r in ja_api ja_scheduler ja_notifier ja_applier ja_readonly; do
  printf '%-14s ' "$r"
  docker compose exec -e PGPASSWORD=$(cat /opt/job-agent/secrets/${r}_password) -T postgres \
    psql -h 127.0.0.1 -U $r -d $DB -tAc 'SELECT count(*) FROM sensitive.profile_sensitive;' 2>&1 | head -1
done
```

**Right:** five lines, each `ERROR: permission denied for schema sensitive`.

And confirm the one role that *should* work does:

```bash
docker compose exec -e PGPASSWORD=$(cat /opt/job-agent/secrets/ja_renderer_password) -T postgres \
  psql -h 127.0.0.1 -U ja_renderer -d $DB -tAc 'SELECT field FROM sensitive.profile_sensitive;'
```

**Right:** `tfn`. A permission error here means Phase 12 will be unable to put your address on
a PDF, and you would not find out for months.

Clean up:

```bash
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB -c \"DELETE FROM app.profile_versions WHERE content_sha256 = repeat('a',64);\""
```

### T7 — Append-only, both layers

```bash
docker compose exec -e PGPASSWORD=$(cat /opt/job-agent/secrets/ja_api_password) -T postgres \
  psql -h 127.0.0.1 -U ja_api -d $DB <<'SQL'
INSERT INTO app.events (entity_type, actor, action) VALUES ('test','t7','probe');
UPDATE app.events SET action = 'tampered' WHERE actor = 't7';
SQL
```

**Right:** `INSERT 0 1` then `ERROR: permission denied for table events`.
**Wrong:** `UPDATE 1` — the `REVOKE` in §9.2 didn't run.

Now the trigger, as the superuser, which the grant cannot stop:

```bash
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB -c \"UPDATE app.events SET action='tampered' WHERE actor='t7';\""
```

**Right:** `ERROR: append-only: UPDATE on app.events is not permitted`.
**Wrong:** `UPDATE 1` — the trigger is missing or disabled. Check with
`SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = 'app.events'::regclass;` — `tgenabled`
must be `O`.

### T8 — Double application is a constraint

```bash
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB" <<'SQL'
INSERT INTO app.job_groups (title) VALUES ('T8 probe');
INSERT INTO app.applications (job_group_id) SELECT id FROM app.job_groups WHERE title='T8 probe';
-- second live application for the same group:
INSERT INTO app.applications (job_group_id) SELECT id FROM app.job_groups WHERE title='T8 probe';
SQL
```

**Right:** the third statement fails with
`ERROR: duplicate key value violates unique constraint "applications_one_live_per_group"`.
**Wrong:** `INSERT 0 1` — the index is not partial, or not there.

Then prove the escape hatch works, because a constraint you can never satisfy again is its own
bug:

```bash
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB" <<'SQL'
UPDATE app.applications SET status='WITHDRAWN'
  WHERE job_group_id = (SELECT id FROM app.job_groups WHERE title='T8 probe');
INSERT INTO app.applications (job_group_id) SELECT id FROM app.job_groups WHERE title='T8 probe';
SELECT status, count(*) FROM app.applications GROUP BY 1;
DELETE FROM app.job_groups WHERE title='T8 probe';
SQL
```

**Right:** the insert succeeds; you see one `WITHDRAWN` and one `DRAFT`.

### T9 — The DRY_RUN guard

```bash
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB" <<'SQL'
SELECT value FROM app.settings WHERE key='dry_run';
INSERT INTO app.job_groups (title) VALUES ('T9 probe');
INSERT INTO app.applications (job_group_id) SELECT id FROM app.job_groups WHERE title='T9 probe';
-- a live (non-dry-run) submission attempt:
INSERT INTO app.application_attempts (application_id, attempt_no, state, dry_run, submit_intent_at)
SELECT a.id, 1, 'SUBMIT_INTENT', false, now()
  FROM app.applications a JOIN app.job_groups g ON g.id=a.job_group_id WHERE g.title='T9 probe';
SQL
```

**Right:** `value` is `true`, then
`ERROR: DRY_RUN is active: refusing to record a live submission attempt`.
**Wrong:** `INSERT 0 1`. Either the trigger is missing, or `app.settings` has no `dry_run` row
— note the function defaults to blocking when the row is absent, so a missing row is safe;
a row set to `false` is not.

Confirm a dry-run attempt is still allowed, then clean up:

```bash
docker compose exec -T postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_password) psql -h 127.0.0.1 -U $SU -d $DB" <<'SQL'
INSERT INTO app.application_attempts (application_id, attempt_no, state, dry_run, submit_intent_at)
SELECT a.id, 1, 'SUBMIT_INTENT', true, now()
  FROM app.applications a JOIN app.job_groups g ON g.id=a.job_group_id WHERE g.title='T9 probe';
DELETE FROM app.job_groups WHERE title='T9 probe';
SQL
```

**Right:** `INSERT 0 1`, then `DELETE 1`.

### T10 — UTC storage, Sydney only at the edges

```bash
make psql-ro
```
```sql
SHOW timezone;
SELECT now() AS stored_utc,
       now() AT TIME ZONE 'Australia/Sydney' AS displayed_sydney;

-- the April duplicated hour: two distinct instants, one wall-clock reading
SELECT '2027-04-04 02:30:00 AEDT'::timestamptz AS first_pass,
       '2027-04-04 02:30:00 AEST'::timestamptz AS second_pass,
       '2027-04-04 02:30:00 AEST'::timestamptz - '2027-04-04 02:30:00 AEDT'::timestamptz AS gap;
```

**Right:** `timezone` is `UTC`; `stored_utc` ends in `+00`; `displayed_sydney` is 10 or 11
hours ahead; `gap` is `01:00:00`.
**Wrong:** `timezone` showing `Australia/Sydney` for a `ja_*` role — the `ALTER ROLE ... SET
timezone` in §5.2 didn't apply, and every timestamp the application writes will be interpreted
against a shifting offset twice a year.

### T11 — `pool_pre_ping` (the §0.6 fix)

This is the direct regression test for the bug you reported.

```bash
make health                                    # baseline: 200
docker compose restart postgres
sleep 20
curl -sk https://192.168.1.115/health | jq     # do NOT restart the api
```

**Right:** `{"api":"ok","database":"ok",...}` on the first curl after Postgres is back.
**Wrong:** `"database": "error: ConnectionDoesNotExistError"` or
`"error: InterfaceError"` persisting across repeated curls while `docker compose ps` shows
postgres healthy. That means the rebuilt image didn't take — confirm with
`docker compose exec api python -c "import job_agent.web.app as m; print(m.engine.pool.__class__)"`
after a request, and re-run `make build && docker compose up -d --force-recreate api`.

### T12 — Connection budget

```sql
SELECT usename, count(*) FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC;
SHOW max_connections;
```

**Right:** `ja_api` at or below 10, total well under 40.
**Wrong:** a `ja_*` role with a climbing count across repeated checks — a pool leak. Compare
against §2 before adding any service.

### T13 — Backup produces something real

```bash
make backup
sudo ls -lh /opt/job-agent/backups/daily/
```

**Right:** `verified: /opt/job-agent/backups/daily/jobagent-....dump.age (N bytes)` with N in
the tens of kilobytes for an empty schema.
**Wrong:** `FAIL: dump is only N bytes` (the `docker compose exec` produced nothing — usually
the wrong `POSTGRES_USER`), or a file that exists but no `verified:` line, which means the
decrypt-and-list check didn't run.

Confirm the file is genuinely encrypted, not just named that way:

```bash
sudo head -c 64 /opt/job-agent/backups/daily/*.dump.age | strings | head -3
```

**Right:** `age-encryption.org/v1`.
**Wrong:** `PGDMP` — that is a plaintext custom-format dump with an `.age` extension.

### T14 — Restore verification

```bash
make restore-test
```

**Right:** `app.sources live=10 restored=10` and `restore verified`.
**Wrong:** `MISMATCH`, or `pg_restore: error: could not execute query` on something other
than roles/ownership. A backup you have not restored is a hypothesis; this is the phase where
it stops being one.

### T15 — Footprint

```bash
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'
free -h
df -h /
```

**Right:** postgres under ~600 MiB of its 1280 MiB, api near 100 MiB, total under 1 GiB,
`/` under 25%. **Wrong:** postgres above 1100 MiB at idle — `shared_buffers` and
`max_connections` are fighting the container limit; check `SHOW shared_buffers` is still
`320MB`.

### T16 — Survives a reboot

**[PVE host]**:

```bash
pct reboot 120
sleep 90
pct exec 120 -- docker compose -f /home/zeyad/job-agent/compose.yaml ps
pct exec 120 -- systemctl list-timers 'pg-*' 'tailscale-cert*' --no-pager
pct exec 120 -- curl -sk https://192.168.1.115/health
```

**Right:** postgres, api and caddy running; three timers listed with future `NEXT` times;
health returns `"role":"ja_api"` and `"schema_version":"0003_applications"`.
**Wrong:** the `migrator` service appearing in `ps` — `profiles: ["tools"]` is missing and a
migration will run on every boot.

---

## 13. Common errors

**`alembic: Multiple head revisions are present`**
A `down_revision` in `migrations/versions/` is `None` where it should name the previous
revision. `grep -H -E '^(revision|down_revision)' migrations/versions/*.py` and fix the chain
by hand. Never resolve this by editing `app.alembic_version`.

**`Can't locate revision identified by '0002_profile_analysis'`**
The database records a revision whose file is gone. Restore the file from git. If it is truly
lost, `alembic stamp <revision>` after recreating the schema state by hand — but treat that as
a last resort, because it decouples the recorded version from reality.

**`permission denied for schema app`**
The role was created before §5.2's `GRANT USAGE ON SCHEMA app` block, or a later migration
added a table before `make grants` ran. Re-run `make grants`; it is idempotent.

**`permission denied for table <newly added>`**
`ALTER DEFAULT PRIVILEGES` only applies to objects created *after* it ran, and only to objects
created by the role it names (`ja_migrator`). If a table was created by the superuser during
debugging, its grants are missing. Re-run `make grants`.

**`permission denied for sequence ...`**
Something in a later phase used `serial` instead of `GENERATED ALWAYS AS IDENTITY`. Either
convert the column, or add `GRANT USAGE ON SEQUENCE`. Prefer converting.

**`FATAL: password authentication failed for user "ja_api"`**
Three candidates, in order of likelihood: the secret file is not mounted into that service
(check `docker compose exec api ls /run/secrets/`); the file has a trailing newline that
`.strip()` should handle but a manual `echo` reintroduced; or the password was regenerated by
re-running §5.1 without the existence guard while the container held the old one.

**`invalid dsn` or connecting to the wrong host**
A `/` or `+` in a password. §0.7 and §7.1. Verify with
`grep -c '[^A-Za-z0-9]' /opt/job-agent/secrets/ja_api_password` — expect `0`.

**`ERROR: append-only: UPDATE on app.events is not permitted` from your own code**
Working as intended. Events are corrected by appending a compensating event, never by editing
one. If a later phase genuinely needs to mutate an events row, the design is wrong, not the
trigger.

**`DRY_RUN is active: refusing to record a live submission attempt` in Phase 14+**
Also working as intended. The attempt must carry `dry_run = true` until `app.settings`
`dry_run` is set to `false` in Phase 19. Do not flip the setting to make a test pass.

**`make backup` produces a 0-byte file**
`docker compose exec` inherits a TTY when run interactively and corrupts binary output.
The script uses `-T` for exactly this reason — if you run `pg_dump` by hand, use `-T` too.

**`age: error: no identity matched any of the recipients`**
The private key at `/opt/job-agent/secrets/backup-age.key` does not correspond to the public
key used at encryption time — usually because `age-keygen` was re-run and overwrote it. Older
archives are unrecoverable without the original key. This is the failure mode §11.1 warns
about.

**`pg_restore: error: role "ja_worker" does not exist` during a restore test**
Expected and harmless with `--no-owner --no-privileges`. On a real recovery, run
`ops/sql/00-bootstrap.sql` before `pg_restore`.

**`could not resize shared memory segment` during a large query**
`shm_size: 256m` on the postgres service, with parallel workers wanting more. Lower
`max_parallel_workers_per_gather` or raise `shm_size` — but raise `mem_limit` alongside it,
since tmpfs counts against the container's memory.

### Troubleshooting order

1. `make health` — what does the API think the role and revision are?
2. `docker compose logs --tail=50 api postgres`
3. `make migrate-status` — is the schema where you think it is?
4. `make psql-ro` then `\dn+` and `\dp app.*` — are the grants what §9 says?
5. `docker compose run --rm migrator alembic current -v`
6. **[PVE host]** `pct rollback 120 pre-phase3` and redo the section

---

## 14. Completion checklist

- [ ] §0 corrections applied: `TAILNET_IP` binding, `default_sni`, `ip_nonlocal_bind` verified, `tailscale-cert.service` literal path, `make build` clean
- [ ] `sudo systemctl start tailscale-cert.service` completes and Caddy actually restarts
- [ ] Eight `ja_*` secret files at mode 600, none containing a non-alphanumeric character
- [ ] `make bootstrap` is idempotent — second run produces no errors
- [ ] `\du ja_*` shows no Superuser / Create role / Create DB attributes (**T1**)
- [ ] `app` and `sensitive` schemas owned by `ja_migrator` (**T2**)
- [ ] `make migrate-status` shows `0003_applications (head)` (**T3**)
- [ ] Full downgrade to base and re-upgrade completes cleanly (**T4**)
- [ ] 32 tables in `app` (31 + `alembic_version`), exactly 1 in `sensitive` (**T5**)
- [ ] `ja_worker` gets `permission denied for schema sensitive` **with a row present** (**T6**)
- [ ] `ja_api`, `ja_scheduler`, `ja_notifier`, `ja_applier`, `ja_readonly` likewise denied (**T6**)
- [ ] `ja_renderer` **can** read it (**T6**)
- [ ] `events` UPDATE refused by grant *and* by trigger (**T7**)
- [ ] Second live application for one job group raises 23505; withdrawal re-permits (**T8**)
- [ ] Non-dry-run `SUBMIT_INTENT` insert is refused by the trigger (**T9**)
- [ ] `SHOW timezone` is `UTC` for a `ja_*` role; DST gap test returns `01:00:00` (**T10**)
- [ ] `/health` recovers on its own after `docker compose restart postgres` (**T11**)
- [ ] `/health` reports `"role":"ja_api"` and the current revision
- [ ] `pg_stat_activity` totals well under 40 (**T12**)
- [ ] `make backup` prints `verified:`; the file begins `age-encryption.org/v1` (**T13**)
- [ ] `make restore-test` prints `restore verified` (**T14**)
- [ ] **The age private key is in your password manager**, off this machine
- [ ] `make seed` applied: 10 sources, 6 settings, 1 rule, 5 companies
- [ ] Idle memory under 1 GiB; postgres under 600 MiB (**T15**)
- [ ] Stack and all three timers return after `pct reboot 120`; `migrator` does **not** run (**T16**)
- [ ] `git add -A && git commit` — migrations, `ops/sql/`, compose and config changes are tracked; `.env` and secrets are not (`git check-ignore .env`)
- [ ] **[PVE host]** `pct snapshot 120 phase3-complete`

---

## 15. Report back

```
Alembic head:            ___________________
Tables in app / sensitive: _______ / _______   (expect 32 / 1)
T6 — ja_worker denial:   ___________________  (exact error text)
T6 — ja_renderer read:   ___________________
T8 — constraint name:    ___________________
T9 — guard message:      ___________________
T11 — /health after postgres restart, no api restart: ______________
Backup file size:        ___________________
restore-test result:     ___________________
Age key stored off-box:  yes / no
Idle memory total:       ___________ MiB
postgres idle:           ___________ MiB
Anything that failed:    ___________________
```

## 16. What Phase 4 inherits

- `app.profile_versions` / `app.profile_facts` / `sensitive.profile_sensitive` exist and are
  empty. Phase 4 writes the YAML schema, the pydantic models, the loader that computes
  `content_sha256`, and the `PublicProfile` projection.
- The projection is the type-system guarantee; `ja_worker`'s lack of schema `USAGE` is the
  database guarantee. Phase 4 should not weaken either by adding a convenience path that reads
  sensitive fields into the worker.
- `ops/sql/10-grants.sql` is re-run after every migration that adds a table. Add that line to
  the Phase 4 checklist.
- The `renderer` service constraint from §4 — `internal` network only, no LLM SDK — is a
  Phase 12 deliverable that is only meaningful if it is not quietly dropped. It is recorded in
  the grant matrix for that reason.

---

**Next:** Phase 4 — the master profile system: YAML schema, pydantic models, stable
provenance IDs on every atomic fact, the loader and validator, and the `PublicProfile`
projection that makes it structurally impossible to serialise your address into a prompt.

Tell me when the checklist is complete, and paste anything that failed.
