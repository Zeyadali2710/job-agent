# Handing the job-agent build to Claude Code

Two parts: **A** is what you do before opening Claude Code, **B** is the prompt you paste in.

Read §A.0 first — there is one decision that determines everything else.

---

## A.0 — Decide where the source of truth lives

Right now the only copy of your working code is inside CT 120 at `/home/zeyad/job-agent`.
It has been edited live, in place, for the entire build. There is no second copy.

**Do not start a Claude Code session that edits a fresh checkout and then try to
reconcile it with CT 120 later.** That is how you lose a night's debugging. Pick one:

- **Option 1 — git, remote-first (recommended).** Push CT 120's repo to a private
  GitHub repo. Clone it locally. Claude Code edits locally, you commit and push, CT 120
  pulls. One direction, always. Requires a GitHub account and CT 120 having egress (it has).
- **Option 2 — git, no remote.** `git bundle` the repo out of CT 120 over scp. Works
  offline, but merging changes back is manual and error-prone. Only if you refuse a remote.

Everything below assumes Option 1.

---

## A.1 — Verify CT 120's repo is clean and complete

**In CT 120**, as `zeyad`:

```bash
cd ~/job-agent
git status --short
```

**Expect** either no output (everything committed) or a list of modified files. If there
are uncommitted changes — there will be, from tonight — commit them first:

```bash
git add -A
git commit -m "phases 1-8: adapters, dedup, classification, all live fixes"
```

**Critical check before pushing anywhere.** Confirm no secrets are tracked:

```bash
git ls-files | grep -E '\.env$|secrets/|sensitive\.yaml' ; echo "exit=$?"
```

**Expect** no output and `exit=1`. If anything is listed, **stop** — you are about to push
your TFN or a database password to GitHub. Remove it from tracking:

```bash
git rm --cached <the-file>
echo "<the-file>" >> .gitignore
git commit -m "remove secret from tracking"
```

Then verify the file's entire history is clean, not just the current commit:

```bash
git log --all --oneline -- .env profile/sensitive.yaml
```

**Expect** no output. If commits are listed, the secret is in history and pushing exposes it
even after deletion. Either rewrite history (`git filter-repo`) or start a fresh repo with
no history — for a personal project of this age, starting fresh is faster and safer.

## A.2 — Confirm what the repo actually contains

```bash
git ls-files | sed 's|/[^/]*$||' | sort -u
```

**Expect** to see `src/job_agent/{core,sources,pipeline,screening,profile,web,workers,scheduler}`,
`migrations/{sql,versions}`, `ops/{sql,caddy,postgres}`, `config`, `tests/unit`.

```bash
git ls-files | wc -l
```

Note that number. You will check it matches after cloning.

## A.3 — Push to a private remote

Create an **empty private** repo on GitHub (no README, no .gitignore — an empty one avoids
a merge conflict on first push). Then:

```bash
git remote add origin git@github.com:<you>/job-agent.git
git branch -M main
git push -u origin main
```

If SSH keys aren't set up in CT 120, use a fine-grained personal access token over HTTPS
instead, scoped to that one repo, with Contents: read/write.

**Verify the push landed:**

```bash
git ls-remote --heads origin
```

**Expect** a `refs/heads/main` line.

## A.4 — Clone locally

On your laptop/desktop:

```bash
git clone git@github.com:<you>/job-agent.git
cd job-agent
git ls-files | wc -l
```

**Expect** the same count as A.2. If it differs, something didn't push.

## A.5 — Recreate the local-only files that are deliberately not in git

The repo has no secrets and no `.env`. Claude Code needs enough to run tests, not to run
the stack. Create a minimal local env:

```bash
cp .env.example .env 2>/dev/null || cat > .env <<'EOF'
POSTGRES_USER=jobagent
POSTGRES_DB=jobagent
DRY_RUN=true
LOG_LEVEL=INFO
TZ=Australia/Sydney
EOF
```

**Do not copy real secrets to your laptop.** The unit tests in `tests/unit` are pure
functions — no database, no network. That is all Claude Code needs to verify its work.
Integration testing stays on CT 120.

---

# B — The prompt

Open Claude Code in the cloned repo and paste everything between the lines.

---

I'm continuing a self-hosted job application agent. The repo you're in is the real,
working codebase — it has been built and debugged live against real data over several
sessions. Treat existing code as correct-until-proven-otherwise; it encodes fixes for
bugs that took hours to find.

## Read these first, in this order

1. `00-ARCHITECTURE.md` — the design. Authoritative on intent.
2. `01-ARCHITECTURE-REVISED-gmktec-m8.md` — hardware-specific revisions.
3. `PHASE-01` through `PHASE-03` and `PHASES-04-09` markdown guides — the build guides.
   **These are partly out of date.** Where a guide and the code disagree, the code wins,
   because the code was corrected against real ATS data and the guides weren't always
   updated. Tell me when you find a disagreement.

## Deployment context you must respect

- Production is an **unprivileged LXC container (CT 120) on Proxmox**, user `zeyad`,
  repo at `~/job-agent`. You are working on a **local clone**. You cannot reach CT 120.
- Everything runs in Docker Compose. Containers run as **UID/GID 10001**. Any file a
  container reads must be group-readable by 10001 — this caused about six separate
  failures during the build.
- **Secrets are files at `/run/secrets/<name>`, mounted from `/opt/job-agent/secrets/`.
  Never environment variables.** They are not in this repo and must never be added.
- `DRY_RUN=true` is wired through env, an in-function assertion, and a database trigger.
  All three stay. Do not add a code path that bypasses any of them.
- Postgres stores and computes in **UTC**. `Australia/Sydney` appears only at display
  boundaries (`syd()` in `web/routes.py`, and the scheduler's cron triggers).
- Container memory limits are enforced and must stay within an 8192 MiB budget. The
  table is in the Phase 2/3 guides. Don't add a service without checking it.

## Database privilege model — read before writing any SQL

Seven least-privilege Postgres roles. The important one: **`ja_worker` has no `USAGE` on
schema `sensitive`**, so the AI code path structurally cannot read the profile's TFN,
visa number, address or referees. `ja_renderer` is the only role that can, and its
container has no network egress.

A repeated bug worth knowing: any role doing `INSERT ... ON CONFLICT DO UPDATE` needs
**`SELECT` as well as `INSERT`/`UPDATE`** on that table, because evaluating the conflict
target reads the row. Four roles were missing this. If you add a grant, include SELECT
wherever an upsert touches the table.

## Where the build actually is

Phases 1–3 complete and verified. Phases 4–8 built and debugged against real data
(~4,000 postings from ~44 companies across Greenhouse, Lever, Ashby, Workable).
Phase 9 (dashboard) written but not yet exercised. Phases 10–19 not started.

## Known-fragile areas — do not "clean these up" without asking

These look like they could be simplified. They can't. Each is a fix for a real bug:

- `sources/urls.py` — `gh_src` is stripped by exact match while `gh_jid` is preserved.
  Do not collapse these back into a `gh_` prefix rule: some employers carry the job
  identity in `gh_jid`, and stripping it collapses every posting to one URL hash.
- `sources/urls.py` — `title_norm` keeps discriminating bracket text and drops only
  year/requisition noise. Stripping all brackets merges different teams' roles.
- `sources/ats/workable.py` — `trust_url_for_hash = False` and the `_loc_key()`
  composite ID. Workable emits one requisition as several entries sharing a shortcode
  and URL, one per location. Without this they overwrite each other.
- `pipeline/dedupe.py` — layers 4/5 refuse to fire when `location_class = 'UNKNOWN'`,
  match only against a group's canonical posting, and compare real `location_raw` text
  for coarse buckets like `REQUIRES_RELOCATION`. Each guard prevents a specific class of
  false merge that was observed in production data.
- `screening/location.py` — location is decided from `location_raw` only, never the
  description. Descriptions mention "remote" and "Australia" in boilerplate constantly.
- `workers/discovery.py` — per-record transactions with try/except. One bad posting used
  to abort an entire poll and silently skip every company after it alphabetically.
- `scheduler/main.py` — jobs use named module-level functions, never lambdas.
  APScheduler's SQLAlchemy jobstore cannot pickle a lambda; a lambda job fails to
  register, sometimes silently.
- `screening/location.py` — POSTCODE must only ever search `location_raw`, never
  `blob`. Descriptions contain years like "founded in 2019", and 2000–2249 is the
  Sydney metro postcode range. This silently classified US and Brazilian jobs as
  Sydney for 24 postings. Regression test: `test_year_in_description_is_not_a_postcode`.
- `pipeline/screen.py` — `UNKNOWN` location routes to REVIEW_REQUIRED, never past the
  gate. The previous `and loc_class != "UNKNOWN"` gave unclassifiable jobs a free pass
  into READY.
- Reclassification is not automatic. `dedupe.resolve()` only computes `location_class`
  when it is `UNKNOWN`, so fixing a classifier bug does NOT update existing rows. After
  any change to `screening/location.py`, run a full reclassify pass, then re-screen.

## Documented limitation, already decided — don't "fix" it

The scheduler calls `add_job(..., replace_existing=True)` on every start, which
recomputes `next_run_time` from the cron trigger. A poll missed during downtime is
**skipped, not caught up**. This is accepted: the `work_items` dedupe key prevents
double-processing, and a missed 30-minute discovery window costs nothing. Changing it
requires preserving stored `next_run_time`, which is deliberate future work.

## How I want you to work

- **Verify, don't assume.** This build lost hours to confident fixes that were wrong
  because a schema was guessed instead of checked. Before writing a parser for any
  external API, fetch a real response and show me the actual fields.
- **Every change gets a test.** `tests/unit` is pure-function only — no DB, no network.
  Run `python -m pytest tests/unit -q` before telling me something works.
- **Tell me what "wrong" looks like.** When you give me a command to run on CT 120,
  state the expected output and at least one failure mode I might mistake for success.
  A test that passes for the wrong reason is worse than one that fails.
- **Small commits, one concern each**, with messages explaining why not what.
- I deploy by pulling on CT 120. Never generate commands that assume you can reach it.

## First task

Read the codebase and the guides, then give me:

1. A short assessment of where Phases 4–9 actually stand versus what the guides claim.
2. Any place the guides and the code disagree, and which you think is right.
3. Anything in the "known-fragile" list above that you think is actually wrong — I'd
   rather hear it now than find out in production.

Don't change anything yet.

---

# C — Getting changes back to CT 120

After Claude Code commits and you push:

**In CT 120:**

```bash
cd ~/job-agent
git status --short
```

**Expect** no output. If there are local modifications, you edited CT 120 directly after
pushing — resolve that before pulling, or you'll hit a merge conflict mid-stack:

```bash
git stash          # or commit them, then push from CT 120 first
```

Then:

```bash
git pull --ff-only origin main
```

`--ff-only` is deliberate: it refuses to create a merge commit. If it fails, the two
copies have genuinely diverged and you need to look before proceeding, not auto-merge.

**Rebuild only what changed.** Every service sharing the Dockerfile needs rebuilding —
this bit the build repeatedly, because `docker compose build <one-service>` leaves the
others on stale images:

```bash
docker compose build
docker compose up -d --force-recreate api worker scheduler
sleep 15
docker compose ps
```

**Expect** all services `Up`, none `Restarting`.

**Verify the running container actually has the new code** — a stale image is the single
most repeated failure in this build:

```bash
docker compose run --rm worker grep -c "<a distinctive string from the change>" \
  /app/src/job_agent/<the-file-you-changed>.py
```

**Expect** a non-zero count.

**If a migration was added:**

```bash
make migrate && make migrate-status
make grants     # idempotent; required after any migration that adds a table
```

**If anything is wrong, roll back:**

```bash
# [PVE host, as root]
pct listsnapshot 120
pct rollback 120 <last-good-snapshot>
```

Take a snapshot before every deploy that touches migrations or compose.

---

# D — Skills and tooling

You asked about "ponytail" and "gsd" — I don't recognise either as a real skill or
plugin, and I'm not going to guess at what they do. If you saw them somewhere specific,
check the name against the source; if they're from a plugin marketplace, search it
directly rather than trusting my memory.

What I can speak to:

**Genuinely useful for this project, in rough priority order:**

- **A Postgres MCP server.** This is the big one. Most of the debugging in this build was
  "run a query in psql, paste the output, reason about it." An MCP server that lets Claude
  Code query the database directly collapses that loop. Point it at a **read-only role**
  (`ja_readonly` already exists and is granted exactly for this) — never a write role.
- **Filesystem MCP**, if Claude Code's built-in file access proves limiting for a repo
  this size.
- **`skill-creator`** (Anthropic-published) if you want to encode this project's
  conventions — the UID 10001 rule, the SELECT-with-upsert grant rule, the
  verify-the-schema-before-parsing rule — into a reusable skill so you stop re-explaining
  them each session.

**Built-in skills that may come up later, not now:** `docx`/`pdf` become relevant at
Phase 12 (resume and cover-letter generation writes real PDFs). `frontend-design` is
relevant if you rework the Phase 9 dashboard beyond the current HTMX version.

**Deliberately not recommended:** anything that auto-applies changes without review.
This project's whole safety model is human-in-the-loop, and the build history shows why —
several "obviously correct" fixes were wrong in ways only visible in real data.

---

# E — Pre-flight checklist

- [ ] CT 120 repo committed, `git status --short` clean
- [ ] `git ls-files | grep -E '\.env$|secrets/|sensitive\.yaml'` returns nothing
- [ ] `git log --all --oneline -- .env profile/sensitive.yaml` returns nothing
- [ ] Private remote created and pushed; `git ls-remote --heads origin` shows `main`
- [ ] Local clone file count matches CT 120's
- [ ] Local `.env` created with non-secret placeholders only
- [ ] No real secrets copied to the local machine
- [ ] `python -m pytest tests/unit -q` passes locally
- [ ] **[PVE host]** snapshot taken before the first pull back into CT 120
