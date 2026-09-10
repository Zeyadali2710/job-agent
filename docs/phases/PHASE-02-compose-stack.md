# PHASE 2 — Project Skeleton, Compose Stack, and the Chromium Gate

Prerequisites: Phase 1 complete, CT 120 running, Docker working as your non-root user.

Everything in this phase runs **inside CT 120** as your normal user unless stated.
Take a snapshot first, on the **host**:

```bash
pct snapshot 120 pre-phase2 --description "before compose stack"
```

---

## 1. Objective

A running Compose stack — Postgres, a minimal API, and Caddy — with:

- every container under an enforced memory and CPU limit
- secrets delivered as files, never as environment variables
- HTTPS with a real certificate, behind authentication
- a `/health` endpoint your Uptime Kuma can watch
- `DRY_RUN=true` wired in from the very first line of config
- **a decided answer** on whether Chromium's sandbox works inside this LXC

That last item is the reason this phase exists now rather than at Phase 15. If the
sandbox can't work here, I want to know tonight — while the answer is a config change —
not in six weeks when the browser is load-bearing.

## 2. Container resource allocation

Final split of the 8192 MiB ceiling:

| Service | mem_limit | cpus | Actual expected |
|---|---:|---:|---:|
| LXC OS + dockerd | (600 MiB) | — | ~380 MiB |
| postgres | 1280 MiB | 1.5 | ~450 MiB |
| api | 512 MiB | 1.0 | ~180 MiB |
| worker *(Phase 5)* | 1024 MiB | 2.0 | ~250 MiB |
| browser *(Phase 15)* | 3072 MiB | 2.0 | ~900 MiB–1.6 GiB |
| scheduler *(Phase 6)* | 256 MiB | 0.5 | ~70 MiB |
| notifier *(Phase 10)* | 192 MiB | 0.5 | ~60 MiB |
| caddy | 128 MiB | 0.5 | ~30 MiB |
| **Total limits** | **7064 MiB** | | **1128 MiB slack** |

Phase 2 starts postgres, api and caddy. The rest are defined in compose now (commented)
so the budget is visible in one place from the beginning.

---

## 3. Repository skeleton

```bash
mkdir -p ~/job-agent && cd ~/job-agent
git init -b main

mkdir -p \
  config profile/{answers/narratives,resumes} \
  src/job_agent/{core,models,profile,sources/{ats,feeds,email},pipeline,screening,ai/prompts,generation,application,automation/adapters,notifications,scheduler,workers,web/{templates,static}} \
  migrations tests/{unit,integration,fixtures,mock_forms} \
  ops/{caddy,browser,backup} \
  docs/{phases,adr}

find src -type d -exec touch {}/__init__.py \;
```

Move the design documents in so the repo is self-describing:

```bash
# copy 00-ARCHITECTURE.md, 01-ARCHITECTURE-REVISED-gmktec-m8.md,
# PHASE-01-lxc-foundation.md and this file into docs/
```

### `.gitignore`

```bash
cat > .gitignore <<'EOF'
.env
.env.*
!.env.example
secrets/
*.key
*.pem
profile/sensitive.yaml
!profile/sensitive.example.yaml
artifacts/
logs/
data/
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
EOF
```

### Secret-leak guard

A pre-commit hook is worth the two minutes — the Discord webhook is the thing most likely
to end up committed by accident:

```bash
cat > .git/hooks/pre-commit <<'EOF'
#!/bin/bash
PATTERNS='discord(app)?\.com/api/webhooks/[0-9]+/|sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
if git diff --cached -U0 | grep -nEi "$PATTERNS"; then
  echo "BLOCKED: possible secret in staged changes." >&2
  exit 1
fi
EOF
chmod +x .git/hooks/pre-commit
```

Test it before trusting it:

```bash
echo "https://discord.com/api/webhooks/123456789/abcdefg" > /tmp/leak.txt
cp /tmp/leak.txt . && git add leak.txt && git commit -m "test"   # must be BLOCKED
git reset && rm leak.txt
```

---

## 4. Secrets

Phase 1 created `/opt/job-agent/secrets` at mode 700. Populate it:

```bash
openssl rand -base64 32 | tr -d '\n' > /opt/job-agent/secrets/postgres_password
printf 'PLACEHOLDER_SET_IN_PHASE_10' > /opt/job-agent/secrets/discord_webhook
printf 'PLACEHOLDER_SET_IN_PHASE_11' > /opt/job-agent/secrets/anthropic_api_key
chmod 600 /opt/job-agent/secrets/*
ls -l /opt/job-agent/secrets/
```

These are mounted into containers as **files** at `/run/secrets/<name>`, not as
environment variables. The distinction matters: env vars are visible in
`docker inspect`, in `/proc/<pid>/environ` to anything sharing the namespace, and in
crash dumps. Files are not. §9 verifies this.

### `.env` — non-secret configuration only

```bash
cat > .env.example <<'EOF'
# ---- global safety ----
DRY_RUN=true

# ---- identity ----
TZ=Australia/Sydney
COMPOSE_PROJECT_NAME=job-agent

# ---- database ----
POSTGRES_USER=jobagent
POSTGRES_DB=jobagent
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

# ---- dashboard ----
DASHBOARD_USER=admin
# generate with: docker run --rm caddy caddy hash-password --plaintext 'yourpassword'
DASHBOARD_PASSWORD_HASH=
# your MagicDNS name, e.g. job-agent.tailXXXX.ts.net
TAILNET_HOSTNAME=

# ---- app ----
LOG_LEVEL=INFO
EOF

cp .env.example .env
chmod 600 .env
```

Fill in `TAILNET_HOSTNAME` (from `tailscale status`) and generate the password hash:

```bash
docker run --rm caddy caddy hash-password --plaintext 'pick-a-strong-password'
```

Paste the `$2a$14$...` output into `DASHBOARD_PASSWORD_HASH`. Because `$` is special in
Compose interpolation, **double every `$`** in the `.env` value: `$2a$14$...` becomes
`$$2a$$14$$...`.

---

## 5. Minimal application

Just enough to have something real to health-check.

### `requirements.txt`

```bash
cat > requirements.txt <<'EOF'
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
pydantic-settings==2.7.0
sqlalchemy[asyncio]==2.0.36
asyncpg==0.30.0
jinja2==3.1.5
structlog==24.4.0
EOF
```

### `src/job_agent/core/config.py`

```python
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret(name: str) -> str:
    """Read a Docker secret file. Fails loudly rather than falling back to env."""
    path = Path("/run/secrets") / name
    if not path.exists():
        raise RuntimeError(f"Required secret '{name}' not mounted at {path}")
    return path.read_text().strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    dry_run: bool = True
    log_level: str = "INFO"
    tz: str = "Australia/Sydney"

    postgres_user: str = "jobagent"
    postgres_db: str = "jobagent"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        pw = _read_secret("postgres_password")
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`dry_run` defaults to `True` in the type definition itself, so a missing or malformed
`.env` fails safe rather than fails open. That property gets asserted inside the submit
function in Phase 14, not just read at the caller.

### `src/job_agent/web/app.py`

```python
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from job_agent.core.config import get_settings

settings = get_settings()
engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=5)
    yield
    await engine.dispose()


app = FastAPI(title="Job Agent", lifespan=lifespan)


@app.get("/health")
async def health():
    checks = {"api": "ok", "database": "unknown", "dry_run": settings.dry_run}
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        return JSONResponse(checks, status_code=503)
    return checks


@app.get("/")
async def index():
    return {"service": "job-agent", "phase": 2, "dry_run": settings.dry_run}
```

The health endpoint returns **503 when the database is unreachable**, so Uptime Kuma
alerts on a half-dead stack rather than a green API in front of a dead Postgres.

### `Dockerfile`

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd -r app --gid 10001 && useradd -r -g app --uid 10001 app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ /app/src/
ENV PYTHONPATH=/app/src

USER app
EXPOSE 8000
CMD ["uvicorn", "job_agent.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 6. Postgres tuning

Sized for the 1280 MiB container limit, not for the host:

```bash
cat > ops/postgres.conf <<'EOF'
listen_addresses = '*'
max_connections = 40

shared_buffers = 320MB
effective_cache_size = 768MB
work_mem = 8MB
maintenance_work_mem = 64MB

# NVMe
random_page_cost = 1.1
effective_io_concurrency = 200

max_wal_size = 1GB
min_wal_size = 128MB
wal_compression = on
checkpoint_completion_target = 0.9

# small dataset — vacuum eagerly, it is cheap here
autovacuum_vacuum_scale_factor = 0.05
autovacuum_analyze_scale_factor = 0.02

timezone = 'UTC'
log_timezone = 'UTC'
log_min_duration_statement = 500
log_checkpoints = on
log_lock_waits = on
EOF
```

Postgres stores and computes in **UTC**; `Australia/Sydney` is applied only at the
scheduling and display boundaries. Mixing those layers is how you end up with an
off-by-one-hour bug twice a year at the AEDT transition.

---

## 7. Caddy

```bash
cat > ops/caddy/Caddyfile <<'EOF'
{
	admin off
	auto_https disable_redirects
}

# Primary: Tailscale MagicDNS name with a real certificate
{$TAILNET_HOSTNAME} {
	tls /certs/cert.pem /certs/key.pem

	# health is unauthenticated: no sensitive data, and monitors shouldn't hold credentials
	handle /health {
		reverse_proxy api:8000
	}

	handle {
		basic_auth {
			{$DASHBOARD_USER} {$DASHBOARD_PASSWORD_HASH}
		}
		reverse_proxy api:8000
	}

	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
		-Server
	}

	log {
		output stdout
		format json
	}
}

# Secondary: direct LAN access by IP, self-signed
https://192.168.1.115 {
	tls internal
	handle /health {
		reverse_proxy api:8000
	}
	handle {
		basic_auth {
			{$DASHBOARD_USER} {$DASHBOARD_PASSWORD_HASH}
		}
		reverse_proxy api:8000
	}
}
EOF
```

`admin off` closes Caddy's admin API, which is otherwise an unauthenticated
config-rewrite endpoint on port 2019.

### Tailscale certificate

`tailscaled` runs in the LXC, not in Docker, so `tailscale cert` works directly:

```bash
sudo mkdir -p /opt/job-agent/certs
sudo tailscale cert \
  --cert-file /opt/job-agent/certs/cert.pem \
  --key-file  /opt/job-agent/certs/key.pem \
  "$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
sudo chmod 644 /opt/job-agent/certs/cert.pem
sudo chmod 640 /opt/job-agent/certs/key.pem
ls -l /opt/job-agent/certs/
```

These expire, so renew on a timer:

```bash
sudo tee /etc/systemd/system/tailscale-cert.service > /dev/null <<'EOF'
[Unit]
Description=Renew Tailscale certificate for job-agent
After=tailscaled.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c '/usr/bin/tailscale cert --cert-file /opt/job-agent/certs/cert.pem --key-file /opt/job-agent/certs/key.pem "$(/usr/bin/tailscale status --json | /usr/bin/jq -r .Self.DNSName | sed "s/\\.$//")"'
ExecStartPost=/bin/bash -c 'cd /home/%i/job-agent 2>/dev/null || cd /root; docker compose restart caddy || true'
EOF

sudo tee /etc/systemd/system/tailscale-cert.timer > /dev/null <<'EOF'
[Unit]
Description=Monthly Tailscale cert renewal

[Timer]
OnCalendar=monthly
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now tailscale-cert.timer
systemctl list-timers tailscale-cert.timer
```

Edit the `ExecStartPost` path to your actual repo location — Caddy does not re-read
certificates from disk on its own, so the restart is what makes renewal take effect.

**If `tailscale cert` fails**, HTTPS certificates are probably not enabled for your
tailnet: admin console → DNS → enable **MagicDNS**, then **HTTPS Certificates**. Until
then, comment out the first site block and use the IP block with `tls internal`.

---

## 8. Compose

```bash
cat > compose.yaml <<'EOF'
name: job-agent

x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

secrets:
  postgres_password:
    file: /opt/job-agent/secrets/postgres_password
  discord_webhook:
    file: /opt/job-agent/secrets/discord_webhook
  anthropic_api_key:
    file: /opt/job-agent/secrets/anthropic_api_key

services:
  postgres:
    image: postgres:17-bookworm
    restart: unless-stopped
    mem_limit: 1280m
    memswap_limit: 1280m
    cpus: 1.5
    shm_size: 256m
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
      TZ: UTC
    secrets:
      - postgres_password
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./ops/postgres.conf:/etc/postgresql/postgresql.conf:ro
    command: postgres -c config_file=/etc/postgresql/postgresql.conf
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    logging: *default-logging
    networks: [internal]

  api:
    build: .
    restart: unless-stopped
    mem_limit: 512m
    memswap_limit: 512m
    cpus: 1.0
    pids_limit: 256
    init: true
    read_only: true
    tmpfs:
      - /tmp:size=64m
    security_opt:
      - no-new-privileges:true
    cap_drop: [ALL]
    environment:
      DRY_RUN: ${DRY_RUN}
      LOG_LEVEL: ${LOG_LEVEL}
      TZ: ${TZ}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_HOST: postgres
    secrets:
      - postgres_password
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=3).status==200 else 1)\""]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging: *default-logging
    networks: [internal]

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    mem_limit: 128m
    memswap_limit: 128m
    cpus: 0.5
    ports:
      - "192.168.1.115:443:443"
      - "100.113.104.18:443:443"
    environment:
      TAILNET_HOSTNAME: ${TAILNET_HOSTNAME}
      DASHBOARD_USER: ${DASHBOARD_USER}
      DASHBOARD_PASSWORD_HASH: ${DASHBOARD_PASSWORD_HASH}
    volumes:
      - ./ops/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - /opt/job-agent/certs:/certs:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - api
    logging: *default-logging
    networks: [internal, edge]

  # ---- defined in later phases; budget shown so the total stays visible ----
  # worker:     mem_limit: 1024m  cpus: 2.0   (Phase 5)
  # scheduler:  mem_limit: 256m   cpus: 0.5   (Phase 6)
  # notifier:   mem_limit: 192m   cpus: 0.5   (Phase 10)
  # browser:    mem_limit: 3072m  cpus: 2.0   (Phase 15)

volumes:
  pgdata:
  caddy_data:
  caddy_config:

networks:
  internal:
    internal: true
  edge:
EOF
```

Three details worth understanding rather than copying:

- **`ports: "192.168.1.115:443:443"`** binds to that address only, not `0.0.0.0`. Docker
  writes its own iptables rules that bypass the PVE bridge firewall, so without the
  explicit bind Docker would happily expose the port on every interface regardless of
  §8 of Phase 1.
- **`internal: true`** on the `internal` network means Postgres and the API have no route
  out to the internet at all. Only Caddy sits on both networks. Later, `worker` will need
  egress and will join `edge` too — that's a deliberate, visible decision per service.
- **`memswap_limit` equal to `mem_limit`** disables swap for the container. A container
  that swaps looks alive to healthchecks while doing nothing; being killed is more honest.

### `Makefile`

```bash
cat > Makefile <<'EOF'
.PHONY: up down logs ps restart build health psql prune
up:      ; docker compose up -d --build
down:    ; docker compose down
logs:    ; docker compose logs -f --tail=100
ps:      ; docker compose ps
restart: ; docker compose restart
build:   ; docker compose build
health:  ; curl -sk https://192.168.1.115/health | jq
psql:    ; docker compose exec postgres psql -U $$(grep POSTGRES_USER .env | cut -d= -f2) -d $$(grep POSTGRES_DB .env | cut -d= -f2)
prune:   ; docker system prune -af --volumes=false && docker builder prune -af
EOF
```

### Start it

```bash
cd ~/job-agent
make up
make ps
```

All three should reach `healthy` or `running` within about 60 seconds.

---

## 9. Verification

### T1 — Resource limits are actually enforced

This is the one that can silently fail in an LXC. If the memory controller isn't delegated
into the container, Docker accepts `mem_limit` and ignores it — and you'd only discover it
when Chromium takes the whole homelab down.

```bash
docker info 2>&1 | grep -iE 'warning|limit'
```

**Expect no warnings about memory limit or swap limit support.** Then prove it:

```bash
docker run --rm --memory=64m --memory-swap=64m python:3.13-slim \
  python -c "x = bytearray(200*1024*1024); print('NOT LIMITED')"
echo "exit=$?"
```

**Expect exit 137** (OOM-killed) and no `NOT LIMITED` output. If it prints `NOT LIMITED`,
stop and tell me — limits are not being enforced and nothing else in this phase is safe.

Confirm the real limits landed:

```bash
docker compose ps -q | xargs docker inspect \
  --format '{{.Name}} mem={{.HostConfig.Memory}} cpus={{.HostConfig.NanoCpus}}'
```

### T2 — Postgres

```bash
make psql
```

```sql
SHOW shared_buffers;          -- 320MB
SHOW max_connections;         -- 40
SHOW timezone;                -- UTC
SELECT version();
\q
```

### T3 — Health endpoint

```bash
make health
```

Expect `{"api":"ok","database":"ok","dry_run":true}`.

Now prove the 503 path works:

```bash
docker compose stop postgres
curl -sk -o /dev/null -w '%{http_code}\n' https://192.168.1.115/health   # expect 503
docker compose start postgres
sleep 15 && make health                                                   # expect ok
```

### T4 — Authentication

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://192.168.1.115/          # expect 401
curl -sk -u admin:yourpassword -o /dev/null -w '%{http_code}\n' https://192.168.1.115/  # expect 200
```

If you get 401 with the correct password, the `$$` escaping in `.env` is wrong — see §11.

### T5 — Certificate

From a device on your tailnet, in a browser:

```
https://job-agent.<tailnet>.ts.net/
```

Expect a padlock with no warning, then a login prompt.

### T6 — Secrets are not exposed

```bash
docker compose exec api env | grep -iE 'password|webhook|api_key'   # expect NOTHING
docker inspect job-agent-api-1 | grep -iE 'password|webhook'        # expect only the secret NAME
docker compose exec api cat /run/secrets/postgres_password          # exists, correct value
docker compose logs | grep -ciE 'discord.com/api/webhooks|sk-ant'   # expect 0
```

### T7 — Network isolation

```bash
docker compose exec api python -c "import socket;socket.create_connection(('1.1.1.1',53),timeout=3)"
```

**Expect this to fail.** The API has no egress by design. Caddy does:

```bash
docker compose exec caddy wget -qO- --timeout=3 https://example.com >/dev/null && echo "caddy has egress"
```

### T8 — Restart survival

```bash
docker compose restart && sleep 30 && make ps && make health
```

Then the real test, from the **host**:

```bash
pct reboot 120
sleep 90
pct exec 120 -- docker compose -f /home/YOURUSER/job-agent/compose.yaml ps
```

All services back automatically, no manual start.

### T9 — Footprint

```bash
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
free -h
df -h /
docker system df
```

Expect total container usage well under 1 GiB at idle, and `/` under 25% used.

---

## 10. The Chromium gate

This decides whether the browser stays in this LXC or gets promoted to its own VM.
Nothing here becomes part of the stack yet — it's a throwaway test.

```bash
cat > /tmp/sandbox_test.py <<'EOF'
import sys
from playwright.sync_api import sync_playwright

args = sys.argv[1:]
try:
    with sync_playwright() as p:
        b = p.chromium.launch(args=args)
        page = b.new_page()
        page.goto("https://example.com", timeout=20000)
        title = page.title()
        page.screenshot(path="/tmp/out/shot.png")
        b.close()
    print(f"LAUNCH OK — title={title!r} args={args}")
except Exception as e:
    print(f"LAUNCH FAILED — {type(e).__name__}: {str(e)[:300]}")
    sys.exit(1)
EOF

mkdir -p /tmp/out
```

### Attempt 1 — full sandbox, default seccomp (the outcome we want)

```bash
docker run --rm --shm-size=1g \
  -v /tmp/sandbox_test.py:/test.py:ro -v /tmp/out:/tmp/out \
  mcr.microsoft.com/playwright/python:latest \
  python /test.py
```

### Attempt 2 — sandbox with SYS_ADMIN, if attempt 1 failed

```bash
docker run --rm --shm-size=1g --cap-add=SYS_ADMIN \
  -v /tmp/sandbox_test.py:/test.py:ro -v /tmp/out:/tmp/out \
  mcr.microsoft.com/playwright/python:latest \
  python /test.py
```

### Attempt 3 — sandbox disabled, if both failed

```bash
docker run --rm --shm-size=1g \
  -v /tmp/sandbox_test.py:/test.py:ro -v /tmp/out:/tmp/out \
  mcr.microsoft.com/playwright/python:latest \
  python /test.py --no-sandbox
```

### Also measure what it costs

While attempt 1 (or whichever succeeded) is running, from a second shell:

```bash
docker stats --no-stream
```

Note the peak. My estimate is 700–950 MB for a simple page; I'd like the real number for
this machine before Phase 15 sets the browser limit.

### Interpreting the result

| Outcome | Meaning | Action |
|---|---|---|
| **Attempt 1 works** | Namespace sandbox functioning inside the unprivileged LXC | Best case. Browser stays here. No compromise. |
| **Attempt 2 works** | Sandbox needs `SYS_ADMIN` on the browser container only | Acceptable. Still unprivileged LXC, still memory-capped, still no secrets volume. |
| **Only attempt 3 works** | No sandbox available | Acceptable **only** with the domain allowlist enforced in code — the browser may load Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday and PageUp and nothing else. Tell me and I'll make that allowlist a hard gate in Phase 15 rather than a config value. |
| **All fail** | Something structural | Send me the error and we go to Option E: a 3 GiB VM for the browser. The design supports it without code changes. |

**Report which attempt succeeded and the peak memory figure.** That determines how Phase 15
is written.

```bash
rm -rf /tmp/sandbox_test.py /tmp/out
```

---

## 11. Common errors

**401 with the correct password**
Compose interpolation ate the `$` characters in the bcrypt hash. Every `$` in
`DASHBOARD_PASSWORD_HASH` must be doubled to `$$`. Verify what Caddy actually received:
`docker compose exec caddy printenv DASHBOARD_PASSWORD_HASH` — it should show single `$`.

**`docker run --memory` doesn't limit anything**
The memory controller isn't delegated into the LXC. Check `pct config 120` for
`features: keyctl=1,nesting=1`, and on the host confirm cgroup v2:
`stat -fc %T /sys/fs/cgroup` should return `cgroup2fs`. Do not continue until T1 passes.

**Postgres: `could not open configuration file`**
The bind mount path is relative to where you run `docker compose`. Run it from
`~/job-agent`, and check `ls -l ops/postgres.conf`.

**Postgres: permission denied on the config file**
The container runs as UID 999. `chmod 644 ops/postgres.conf`.

**`tailscale cert` fails with "HTTPS is not enabled"**
Tailscale admin console → DNS → enable MagicDNS, then HTTPS Certificates.

**Caddy: `bind: cannot assign requested address`**
`192.168.1.115` isn't up on `eth0` inside the container. `ip a show eth0`.

**Port 443 already in use**
`ss -tlnp | grep :443` — something else in the container has it.

**`api` restarts in a loop**
`docker compose logs api`. Most likely `RuntimeError: Required secret 'postgres_password'
not mounted` — the file is missing or `secrets:` is absent from the service definition.
That failure is deliberate; the app refuses to start rather than fall back to a default.

**Build fails: no space / slow**
`docker system df`, then `make prune`.

---

## 12. Uptime Kuma upgrade

In CT 100, edit the monitor from Phase 1:

- Type: **HTTP(s)**
- URL: `https://192.168.1.115/health`
- Interval: 60 s, Retries: 2
- **Ignore TLS/SSL errors: ON** (the IP block uses Caddy's internal CA)
- **Accepted status codes: `200-299`** — so the 503 from a dead database fires an alert
- Notification: your Discord channel

Test it end to end:

```bash
docker compose stop postgres
```

Wait ~2 minutes for a Discord alert, then `docker compose start postgres` and confirm the
recovery message. An alerting path you haven't deliberately broken is not a tested
alerting path.

---

## 13. Completion checklist

- [ ] Repo skeleton created, `git log` shows an initial commit
- [ ] Pre-commit hook **tested** and blocks a fake webhook URL
- [ ] `/opt/job-agent/secrets/` holds three files at mode 600
- [ ] `.env` at mode 600, `DRY_RUN=true`, and **not** tracked by git (`git check-ignore .env`)
- [ ] `make up` brings all three services to healthy
- [ ] **T1 passes** — `--memory=64m` test exits 137, `docker info` shows no limit warnings
- [ ] `shared_buffers` = 320MB, `timezone` = UTC
- [ ] `make health` returns `database: ok`, `dry_run: true`
- [ ] `/health` returns 503 with Postgres stopped
- [ ] `/` returns 401 without credentials, 200 with them
- [ ] `https://job-agent.<tailnet>.ts.net/` loads with a valid certificate
- [ ] `docker compose exec api env` shows no secret values
- [ ] API has **no** internet egress; Caddy does
- [ ] `ss -tlnp` on the container shows 443 bound to `192.168.1.115` only, not `0.0.0.0`
- [ ] Stack returns automatically after `pct reboot 120`
- [ ] Idle memory under 1 GiB; `/` under 25%
- [ ] `tailscale-cert.timer` enabled and listed
- [ ] Uptime Kuma HTTP monitor green, and a **deliberately triggered** Discord alert received
- [ ] **Chromium gate result recorded** — which attempt succeeded, and peak memory
- [ ] Snapshot: `pct snapshot 120 phase2-complete`

## 14. Report back

```
Chromium gate:        attempt ___ succeeded
Browser peak memory:  ___________ MiB
Host free -h total:   ___________ (post-BIOS)
Idle container total: ___________ MiB
Tailnet hostname:     ___________________
Anything that failed: ___________________
```

---

**Next:** Phase 3 — the full schema, Alembic migrations, least-privilege Postgres roles
(including the grant that makes it impossible for the AI code path to read your sensitive
profile fields), the append-only `events` table, and the partial unique index that makes
double-application a database constraint rather than a hope.
