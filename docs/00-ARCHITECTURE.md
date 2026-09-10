# Job Agent — Architecture & Implementation Roadmap

Self-hosted Australian job discovery, matching, and application system.
Target environment: Proxmox VE mini-PC homelab, Sydney, single operator.

---

## 0. Scope corrections you need to read before Phase 1

Three parts of the original brief do not survive contact with reality. The design
below works around them rather than pretending otherwise.

### 0.1 The big boards cannot be scraped, and this is the design's main constraint

SEEK's terms are explicit. You must not use "any automated process, script, tool, bot,
scraper or other technical means" to access or collect information from the service,
"except where we have provided a specific technical interface (such as an API) for
such use." SEEK's developer portal API exists only for hirers and ATS partners — there
is no job-seeker search API. LinkedIn and Indeed are the same or stricter (Indeed
closed its publisher API to new applicants years ago).

This is not a "risk" you can mitigate with proxies and user-agent rotation. It is a
hard boundary, and evading it is also the thing most likely to get your personal SEEK
and LinkedIn accounts terminated — the accounts you actually need to get hired.

**What this means:** the system's primary discovery layer is ATS-first, not
board-first. See §5. Boards are consumed through channels they themselves operate:
their own email alerts.

### 0.2 The 30-minute freshness requirement is mostly unachievable and mostly not valuable

- Reliable posting timestamps exist on roughly the ATS sources only. Aggregators give
  you day-granularity or relative strings ("2 hours ago") and lag the original posting
  by hours to days. You cannot filter to "posted in the last 30 minutes" on data that
  doesn't carry minute-precision timestamps.
- Australian graduate programs — CommBank, Macquarie, Atlassian, Canva, Telstra, the
  Big 4, ASD/DSTG — are almost all **batch reviewed after the closing date**, or use
  fixed assessment waves. Applying 12 minutes after posting confers nothing.
- Where speed genuinely helps is high-volume junior/mid roles at companies that screen
  on arrival. There, being in the first day is what matters, not the first half hour.

**The 30-minute poll cadence is still worth building** — it gives you a same-day feed
and a real-time Discord alert, which is a genuine edge over checking SEEK each evening.
The system just must not *reject* jobs for being 40 minutes old. Freshness becomes a
scoring input and a notification trigger, not a hard gate. Default freshness window in
config: 24 hours, with `posted_at_confidence` recorded so you can tighten it per source.

### 0.3 Unattended submission is the highest-risk, lowest-value component

Mode 3 (fully automatic submit) is buildable and the design supports it, but I'd
recommend against enabling it broadly, for reasons that are about your outcomes rather
than about rules:

- Australian graduate recruiting is a small world. Grad recruiters at the major banks,
  consultancies and tech firms talk to each other and increasingly run similarity
  detection across application free-text. A batch of near-identical LLM cover letters
  across twelve employers is detectable and is remembered.
- Many application flows include a declaration that the submission is your own work.
  An automated submission that ticks that box is a lie the system told on your behalf,
  and it's the exact category of thing that gets an offer rescinded at background-check
  stage.
- Half the funnel isn't a form anyway: video interviews (HireVue, Sonru), psychometrics
  (SHL, Cut-e, Revelian), and coding assessments all require you. Automating the form
  saves you 6 minutes and gates you into the part you must do manually regardless.
- Grad applications are won on the free-text answers. Volume is not the lever;
  per-application quality is.

**Recommended default: Mode 2 (Approval).** The system does 95% of the work — finds it,
scores it, drafts it, fills the form, and stops at the submit button with a screenshot
and a diff for you to approve. You click approve on your phone. Realistic throughput is
still 20–40 quality applications a week with maybe 90 seconds of your attention each.

Mode 3 is implemented, per-company allowlisted, and off by default. If you enable it,
enable it for a named list of employers you've decided to accept the risk for.

I'll build all of it. I'm not going to write code specifically designed to defeat
SEEK's or LinkedIn's bot detection, and the browser layer will refuse to touch CAPTCHA,
MFA or rate-limit controls — but that's the only thing I'm declining, and it's the same
line you drew yourself in the brief.

---

## 1. High-level architecture

```
┌─────────────────────────────────── PROXMOX VE HOST ───────────────────────────────────┐
│                                                                                       │
│  ┌──────────────────── VM 110: job-agent (Debian 13, 4 vCPU / 12 GB) ───────────────┐ │
│  │                                                                                  │ │
│  │   ┌─────────────┐   ┌──────────────────────────────────────────────────────┐     │ │
│  │   │   caddy     │──▶│  api  (FastAPI + Jinja/HTMX dashboard, uvicorn)      │     │ │
│  │   │  TLS, auth  │   └───────────────────────┬──────────────────────────────┘     │ │
│  │   └──────┬──────┘                           │                                    │ │
│  │          │ tailscale0 only                  │                                    │ │
│  │          │                     ┌────────────▼───────────┐                        │ │
│  │          │                     │      PostgreSQL 17     │◀── queue (SKIP LOCKED) │ │
│  │          │                     │  jobs/apps/profile/evt │    + all state         │ │
│  │          │                     └────────────▲───────────┘                        │ │
│  │          │                                  │                                    │ │
│  │   ┌──────┴──────┐   ┌──────────────┐  ┌─────┴────────┐  ┌──────────────┐         │ │
│  │   │  scheduler  │──▶│  discovery   │─▶│   pipeline   │─▶│  applier     │         │ │
│  │   │ APScheduler │   │  workers     │  │  worker      │  │  worker      │         │ │
│  │   │  */30 min   │   │ ATS/RSS/IMAP │  │ dedupe→rules │  │  Playwright  │         │ │
│  │   └─────────────┘   └──────────────┘  │ →LLM→score   │  │  + artifacts │         │ │
│  │                                       └──────────────┘  └──────────────┘         │ │
│  │                            ┌──────────────────────┐                              │ │
│  │                            │  notifier worker     │  ← transactional outbox       │ │
│  │                            │  Discord webhook     │    (never blocks pipeline)    │ │
│  │                            └──────────────────────┘                              │ │
│  │                            ┌──────────────────────┐                              │ │
│  │                            │  ollama (optional)   │  local LLM for PII-bearing    │ │
│  │                            └──────────────────────┘  + high-volume classification │ │
│  └──────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                       │
│  ┌── LXC 111: backup target / PBS client (optional) ──┐   vzdump → NAS/external disk   │
└───────────────────────────────────────────────────────────────────────────────────────┘
        │                    │                     │                    │
   Tailscale            ATS public APIs      Anthropic/OpenAI      Discord webhook
   (your phone)         + gov RSS + IMAP     (redacted payloads)   (outbound only)
```

Nothing listens on the LAN or the internet. All ingress is over Tailscale.

---

## 2. Technology stack and why

| Layer | Choice | Rejected alternative | Reason |
|---|---|---|---|
| Virtualisation | **1 KVM VM**, Docker inside | LXC + Docker | Docker-in-LXC needs nesting/keyctl/AppArmor workarounds and is not supported by Proxmox. You're also running a headless Chromium against arbitrary web pages — kernel-level isolation is worth ~400 MB of overhead. One VM also snapshots and restores as a single unit. |
| Language | **Python 3.13** | Node/TS, Go | Playwright, pydantic, httpx, selectolax, LLM SDKs, and every ATS client are first-class. You're the only maintainer; one language across scraping, AI, and web. |
| Web/API | **FastAPI + uvicorn** | Django | Async matters here (many concurrent HTTP fetches), pydantic models double as your validation layer, and you don't need Django's admin/ORM weight. |
| Dashboard | **Jinja2 + HTMX + Tailwind (CDN or built once)** | React / Next.js | This is the choice I'd most defend. A separate SPA means a second runtime, a build pipeline, a CORS/auth surface, and a second thing to update at 11pm. The dashboard is read-mostly with a handful of POST buttons (approve/reject/retry). HTMX gives you live-updating panels with `hx-trigger="every 30s"` and zero build step. Ships as one container. |
| Database | **PostgreSQL 17** | SQLite | You need concurrent writers (4 workers), `pg_trgm` for fuzzy dedup, JSONB for raw source payloads, `FOR UPDATE SKIP LOCKED` for the queue, and full-text search over descriptions. SQLite gives you none of those cleanly under concurrency. Cost: ~200 MB RAM. |
| Queue | **Postgres `SKIP LOCKED` + APScheduler** | Celery + Redis | Dropping Redis and Celery removes two services, one broker protocol, and a whole class of "task acked but lost" bugs. Work items live in the same transaction as the job row, so a crashed worker can never leave a job half-committed. Debugging is `SELECT * FROM work_items WHERE state='FAILED'`. At your throughput (hundreds of items/day) Postgres is enormously over-provisioned. Revisit only if you exceed ~50 items/second. |
| Scheduler | **APScheduler in a dedicated container**, jobstore in Postgres | cron, systemd timers | Survives restarts with misfire handling (`coalesce=True`, `misfire_grace_time`), timezone-aware (`Australia/Sydney`, handles AEST/AEDT correctly), and introspectable from the dashboard. |
| Browser | **Playwright (Python, Chromium)** | Selenium, Puppeteer, browser-use | Playwright: auto-waiting (kills 80% of flake), `BrowserContext` isolation per application, built-in `tracing` producing a replayable `.zip` for every failure, `codegen` to author new form adapters in minutes, and native file-upload handling. Selenium is more brittle and more verbose. Puppeteer would drag Node in. **browser-use / AI-driving-the-browser: rejected as the submit path** — non-deterministic behaviour on a button that legally binds you is the wrong trade. It's included only as an offline *suggester* that proposes field→profile mappings, which you review once per employer and freeze into a deterministic adapter. |
| Reverse proxy | **Caddy** | Nginx | Automatic internal TLS with a local CA, 6-line config, native `forward_auth`. Nginx buys you nothing here. |
| Remote access | **Tailscale** | Cloudflare Tunnel, port-forward+VPN | Zero open ports, MagicDNS, works from your phone, ACLs, free tier. Cloudflare Tunnel puts your dashboard behind a third party you don't need. |
| Secrets | **Docker secrets from a root-owned `.env`, `SOPS`+age for anything in git** | Vault | Vault is a service to babysit. SOPS+age gives you encrypted-at-rest config you can commit, decrypted only on the VM. |
| Config | **YAML files, git-tracked, schema-validated with pydantic** | DB-only settings | You want to hand-edit and diff your criteria. |

---

## 3. Proxmox architecture

**One VM, not containers.** Specifics:

| Resource | Value | Notes |
|---|---|---|
| VMID | 110 | |
| OS | Debian 13 (trixie) genericcloud | Cloud-init, small, no desktop |
| vCPU | 4 (`host` type) | Chromium wants 2 during a run |
| RAM | 12 GB (balloon min 4 GB) | 8 GB workable without local LLM; 16 GB if you run Ollama with a 8B model |
| Disk | 64 GB on SSD-backed storage, `discard=on,ssd=1` | Postgres + Playwright traces + PDFs |
| Network | virtio on vmbr0, DHCP + reservation | |
| Guest agent | enabled | Needed for clean shutdown and backup fsfreeze |
| Autostart | `onboot=1`, `startup order=1,up=30` | Survives host reboot |

**Backups:** `vzdump` nightly at 03:15 Sydney to a second disk or NAS, mode `snapshot`,
compression `zstd`, 14 daily / 8 weekly retention. Add Proxmox Backup Server later if
you want dedup and incremental. Take a manual `qm snapshot` before each phase's changes
— that's your undo button for the whole build.

**Why not a second VM for the browser?** Not worth it at this scale; the browser runs in
its own container as a non-root user with `--cap-drop=ALL`, seccomp on, and no access to
the secrets volume. If you later want stronger isolation, the applier worker is designed
to be pull-only from the Postgres queue, so it can be lifted into its own VM unchanged.

---

## 4. Data flow

```
 [1] DISCOVER (every 30 min, jittered ±90s per source)
     source adapters → raw_postings (immutable JSONB, never edited)
                       ↓
 [2] NORMALISE       → parse title/company/location/salary/dates → postings
                       posted_at + posted_at_confidence: EXACT|DAY|RELATIVE|INFERRED|UNKNOWN
                       ↓
 [3] DEDUPE          → resolve to job_group via 5-layer key ladder (§10)
                       ↓  (new groups only continue)
 [4] HARD SCREEN     → deterministic rules: work rights, clearance, seniority,
                       location, employment type, company blacklist.
                       Cheap. Kills ~80% before any token is spent.
                       ↓
 [5] ENRICH (LLM)    → structured extraction on survivors only:
                       requirements, skills, work-right clause, clearance clause,
                       salary components, grad-intake metadata → job_analyses
                       ↓
 [6] SCORE           → deterministic weighted score + bounded LLM adjustment (±10)
                       → job_scores with per-feature evidence rows
                       ↓
 [7] ROUTE           → mode + rules → DISCOVERED | SCREENED_OUT | REVIEW_REQUIRED
                       ↓
 [8] GENERATE        → tailored resume (selection/reordering only) + cover letter
                       + answers, each assertion carrying profile source_ids
                       → provenance validator rejects unsourced claims
                       ↓
 [9] APPROVE         → dashboard (or auto, if rules + allowlist + mode 3)
                       ↓
[10] SUBMIT          → Playwright adapter → pre-submit screenshot → intent row
                       flushed → click → confirmation capture → artifacts
                       ↓
[11] RECORD          → applications.status, events append-only, Discord outbox
```

Steps 1–3 are cheap and always run. Step 5 onward costs money and is gated.

---

## 5. Australian job-source strategy

Three tiers, ordered by legitimacy and data quality. Build tier 1 first; it is the bulk
of the value.

### Tier 1 — ATS public job-board APIs (primary, build first)

These are documented, keyless, JSON, and rate-limit-friendly. They are the *same
endpoints the company's own careers page calls in your browser*, published so third
parties can embed the board. Critically, they carry **exact posting timestamps** and,
for Greenhouse, the **application form's question list** — so you can tell before
opening a browser whether you can answer everything.

| ATS | Endpoint | Notes |
|---|---|---|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `first_published`, `updated_at`; `?questions=true` on the single-job endpoint returns the full form schema |
| Lever | `GET https://api.lever.co/v0/postings/{slug}?mode=json` | `createdAt` epoch ms |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | Structured comp when the employer opts in; skip `isListed:false` |
| Workable | `GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true` | Verify per-account; some return empty |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{id}/postings` | Common in AU enterprise |
| Workday | `POST https://{tenant}.wd3.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | Used by CommBank, Woolworths, Telstra, most ASX50. Public search endpoint; JSON body `{limit, offset, searchText, appliedFacets}` |
| PageUp | tenant-specific | Very common in AU universities and government |
| Personio | `GET https://{token}.jobs.personio.de/xml` | XML |

**The work here is building the company→(ATS, tenant token) registry.** That's a curated
YAML file of ~150–300 Australian employers you'd actually work for — ASX-listed tech and
banks, Atlassian, Canva, Safety Culture, Airwallex, Immutable, WiseTech, Rokt, Culture
Amp, Linktree, Domain, REA, Xero AU, the consultancies, the cyber firms (CyberCX,
Tesserent, Secure Code Warrior), the big scale-ups. Two evenings of work, and it becomes
the highest-signal, lowest-noise, fully-legitimate job feed you can have. Discover a
company's ATS by loading its careers page and watching the network tab, or by the URL
shape (`boards.greenhouse.io/x`, `jobs.lever.co/x`, `x.wd3.myworkdayjobs.com`).

### Tier 2 — Government and institutional feeds

| Source | Access | Notes |
|---|---|---|
| APS Jobs (apsjobs.gov.au) | Salesforce Experience Cloud; no public API/RSS. **Use its own email alerts** → IMAP ingestion | Most APS roles require Australian citizenship — the work-rights screen will do heavy lifting |
| I Work For NSW | Check for RSS/JSON on the search endpoint during Phase 4; otherwise email alerts | Big NSW tech/cyber employer |
| Defence / DSTG / ASD | Own portals + APS Jobs | Citizenship + NV1/NV2 gates; often long lead times |
| University careers portals | Mostly PageUp/NGA; check per institution | UNSW alumni access is worth wiring in |

### Tier 3 — Aggregators, consumed legitimately

- **Adzuna API** (`developer.adzuna.com`, free App ID + key, `country=au`) is the one
  aggregator with a real public API. Caveats you must design around: the free tier is
  roughly **1,000 calls/month ≈ 33/day**, which is *less than your 48 daily polls* — so
  Adzuna runs on a **6-hourly** schedule with 2–3 saved queries, not every 30 minutes.
  Descriptions are **truncated**, and salaries may be **modelled, not stated** (check the
  `salary_is_predicted` flag — never display a predicted salary as the employer's number).
  Treat Adzuna as a *discovery hint*: it tells you a role exists; you then resolve the
  real posting via its `redirect_url` and, where it lands on an ATS you support, pull the
  full record from tier 1.
- **SEEK / LinkedIn / Indeed / Jora / GradConnection / Prosple:** no automated access.
  Instead, **set up saved-search email alerts on each of them, pointed at a dedicated
  mailbox** (e.g. a `+jobs` alias or a mailbox you own), and have the system ingest that
  mailbox over IMAP. This is first-party data the platform chose to send you, it's
  entirely within their terms, it's free, and alert emails typically arrive within an hour
  of posting. Parse the alert for title/company/URL, then attempt tier-1 resolution;
  if the URL is a board URL you can't fetch, the job still appears in your dashboard as a
  **manual-apply card** with everything drafted and a link. You click through and paste.

That last pattern is the key architectural move: **the system never needs to fetch a page
it isn't allowed to fetch, and you still get SEEK coverage.**

### Source metadata to record for each adapter

`api_available`, `feed_available`, `access_method`, `has_exact_timestamp`,
`timestamp_confidence`, `poll_interval`, `rate_limit`, `application_method`,
`ats_platform`, `automation_policy` (`AUTO_OK` / `DRAFT_ONLY` / `MANUAL_ONLY`),
`reliability_score`, `duplicate_likelihood`. `automation_policy` is enforced in code:
a `MANUAL_ONLY` source can never reach the submit path regardless of mode.

---

## 6. Australian job-filtering strategy

All of this lives in `config/criteria.yaml`, hot-reloadable, schema-validated.

### 6.1 Location taxonomy

Normalise free text to a structured location, then classify:

```
SYDNEY_CBD | SYDNEY_METRO | GREATER_SYDNEY | NSW_REGIONAL |
REMOTE_AU | REMOTE_GLOBAL | HYBRID_SYDNEY | OTHER_CAPITAL | REQUIRES_RELOCATION | UNKNOWN
```

Implementation: a gazetteer of NSW suburbs/LGAs + postcode ranges (2000–2249 Sydney
metro, 2250–2263 Central Coast, etc.) plus regex for the capitals, plus remote/hybrid
keyword detection ("work from home", "WFH", "hybrid — 3 days in office", "fully remote").
`OTHER_CAPITAL` scores positively only if `allow_relocation: true` in config. Hybrid
gets a `days_in_office` extraction where stated.

### 6.2 Seniority classification

Two-stage: title regex first (cheap, ~90% accurate), LLM only on ambiguity.

- **Target:** `graduate`, `grad program`, `new grad`, `early career(s)`, `emerging
  talent`, `junior`, `entry[- ]level`, `associate`, `trainee`, `cadet`, `intern`,
  `L1`, `SE1`, `Software Engineer I`, `campus hire`
- **Negative:** `senior`, `staff`, `principal`, `lead`, `head of`, `manager`,
  `director`, `architect`, `L4+`, `II`/`III` where paired with years
- **Trap to handle:** "Associate Director" and "Associate Partner" are senior. Require
  `associate` to *not* be followed by a senior noun.
- Cross-check against `min_years_experience` extracted from the body. Title says
  "Graduate" but body says "5+ years" → flag `SENIORITY_CONFLICT`, route to review.

### 6.3 Graduate-program detection

Beyond title matching, extract and store: `intake_year`, `application_deadline`,
`assessment_stages`, `expected_start_date`, `degree_requirements`,
`graduation_window` (e.g. "must have graduated within the last 2 years" — a real and
common AU criterion that can *exclude* you if you graduated too long ago, so it's a
two-sided check), `citizenship_requirement`, `pr_accepted`, `clearance_requirement`,
`wam_gpa_requirement`. Grad programs get their own dashboard view because deadlines,
not freshness, are what matter for them — the system should nag you at deadline−7d.

### 6.4 Work-rights classification

`profile.work_rights.status` is the source of truth. The system never guesses it.

Classifier output (rules first, LLM only for ambiguous prose):

| Class | Trigger examples | Action |
|---|---|---|
| `COMPATIBLE` | "full working rights in Australia", "unrestricted work rights", no restriction stated + AU-domiciled employer | proceed |
| `LIKELY_COMPATIBLE` | "citizens or PR preferred" (preference, not requirement) | proceed, note concern |
| `UNCERTAIN` | no mention at all, or contradictory clauses | proceed if `uncertain_policy: proceed_with_flag` (default), surfaced as a concern in the Discord card |
| `SPONSORSHIP_POSSIBLE` | "sponsorship available for the right candidate", "482 available" | proceed only if profile requires sponsorship |
| `INCOMPATIBLE` | "must be an Australian citizen", "must hold PR", "no sponsorship / no visa holders" **and** your status doesn't meet it | hard block |

Never auto-reject for ambiguity — that's the explicit instruction and it's also correct,
because a large share of AU ads simply omit the clause.

### 6.5 Security-clearance classification

Detect `Baseline`, `NV1`, `NV2`, `PV`/`Positive Vetting`, `TSPV`, `AGSVA`, `Defence
clearance`, `Australian Government security clearance`, `must be able to obtain a
clearance`, `AGSVA sponsorship provided`.

Then classify requirement strength: `MANDATORY_HELD` / `MANDATORY_OBTAINABLE` /
`PREFERRED` / `SPONSORED_AFTER_HIRE` / `UNCLEAR`.

Rule: AGSVA clearances require Australian citizenship, full stop. So
`clearance_required AND profile.citizenship != AU` is a hard block with reason
`CLEARANCE_CITIZENSHIP_CONFLICT`, no LLM call needed. `MANDATORY_OBTAINABLE` +
AU citizen = fine, and is actually a *positive* signal for a grad (they'll sponsor it).
`PREFERRED` never blocks.

### 6.6 Salary normalisation

Parse into structured fields rather than a single number:

```yaml
base_salary_min: 75000
base_salary_max: 85000
super_included: false        # "+ super" → false; "package" → true
super_rate: 0.12             # SG rate at time of posting; configurable
bonus_stated: null
total_package_min: 84000     # derived: base * (1 + super_rate) when super excluded
ote: null                    # keep separate; never compare OTE to base
currency: AUD
period: ANNUAL               # ANNUAL | DAILY | HOURLY
confidence: STATED           # STATED | PREDICTED | INFERRED | ABSENT
```

Regex ladder handles `$70,000`, `$70k`, `$70,000 + super`, `$80k package`,
`$90k - $110k`, `$120k OTE`, `$650/day`, `$45/hr`. Daily/hourly are converted with a
configurable 220 working days / 38 hour week for *comparison only*, and flagged so
you're never shown a contract day rate as an annual salary.

Comparison rule: always compare **base to base** and **package to package**. A `$80k
package` role pays less than a `$78k + super` role. The scorer uses `total_package_min`
with `super_rate` applied, and shows both figures in the UI.

Config: `salary_min`, `salary_target`, `salary_is_mandatory` (default `false` — ~70% of
AU grad ads don't state salary, and mandating it would delete most of your feed).

---

## 7. Discord notification architecture

**Webhook, not a bot.** A webhook is one URL, no gateway connection to keep alive, no
token to rotate, no intents, no uptime requirement. The only thing a bot buys you is
interactive approve/reject buttons, which costs you a persistent WebSocket, a public
HTTPS interactions endpoint (defeating the "nothing exposed" security model), and
signature verification. Not worth it. **Approval happens in the dashboard**, which is
one Tailscale tap away on your phone; the Discord card deep-links straight to it.

### Transactional outbox — the reliability core

Discord must never be able to fail an application. Implementation:

1. The pipeline writes a row to `notifications` **inside the same transaction** as the
   state change that caused it. If the transaction rolls back, so does the notification.
2. A separate `notifier` worker polls `notifications WHERE state='PENDING' FOR UPDATE
   SKIP LOCKED`, posts to the webhook, and marks `SENT` or increments `attempts`.
3. Failures back off exponentially (1m, 5m, 15m, 1h, 6h), cap at 8 attempts, then
   `DEAD` — visible on the dashboard, never retried into a loop.
4. Rate limiting: respect `X-RateLimit-Remaining` / `Retry-After` from Discord; a
   token bucket caps to 5 messages per 2 seconds per webhook.
5. Deduplication: `dedupe_key` unique index (e.g. `high_match:{job_group_id}`) so a
   restart mid-batch can't double-post.

The pipeline is therefore *never* blocked on network I/O to Discord. Discord being down
for a day means you get a burst of backlog when it returns.

### Secret handling

`DISCORD_WEBHOOK_URL` is read from a Docker secret mounted at `/run/secrets/discord_webhook`
(file, not env var, so it doesn't appear in `docker inspect` or `/proc/<pid>/environ`).
A logging filter redacts any string matching `discord(app)?\.com/api/webhooks/\S+` before
it reaches a handler. The dashboard shows `configured: true` and the last-send status,
never the URL. `.env` is `600 root:root`, in `.gitignore`, and the repo has a
`gitleaks` pre-commit hook.

### Notification types

| Event | Default | Content |
|---|---|---|
| `HIGH_MATCH_JOB` | on, score ≥ 80 | full card (below) |
| `APPROVAL_REQUIRED` | on | job + score + drafted-doc status + deep link |
| `APPLICATION_SUBMITTED` | on | confirmation + timestamp AEST |
| `APPLICATION_FAILED` | on | reason + trace link |
| `MANUAL_INTERVENTION_REQUIRED` | on | what to do, explicitly |
| `DEADLINE_APPROACHING` | on | grad programs, T−7d and T−2d |
| `SYSTEM_ERROR` | on, throttled 1/hour/class | |
| `SYSTEM_OFFLINE` | on | dead-man's switch: a separate host-level cron pings a healthcheck; silence for 2 cycles alerts |
| `DAILY_SUMMARY` | on, 19:00 Sydney | counts + top match |
| `WEEKLY_SUMMARY` | on, Sun 18:00 | funnel + response rates |

Card format follows your spec — embed with colour keyed to score band (green ≥85,
yellow 70–84, grey <70), fields for company/location/type/salary/posted, a "why it
matches" list drawn from the top 5 positive score features, a "concerns" list from
negative features and any `UNCERTAIN` classifications, and two links: the job and the
dashboard detail page.

---

## 8. Security model

| Concern | Control |
|---|---|
| Network exposure | Nothing bound to 0.0.0.0. Caddy binds to the `tailscale0` address only. `ufw default deny incoming`, allow only `tailscale0` and (optionally) SSH from LAN. No port forwards on your router. |
| Remote access | Tailscale, with ACLs limiting your phone to tcp:443 on the node. Optional: Tailscale Funnel is **not** used. |
| Dashboard auth | Caddy `basic_auth` with a bcrypt hash **plus** Tailscale identity headers (`Tailscale-User-Login`) verified by a middleware. Two independent factors; neither alone grants access. |
| TLS | Caddy internal CA, cert for `job-agent.<tailnet>.ts.net`. Real cert via Tailscale's HTTPS certs if you want no browser warnings. |
| Secrets | Docker secrets (files) for webhook, DB password, LLM keys. `.env` 600 root. SOPS+age for anything committed. Never in env vars for the browser container. |
| DB | Postgres bound to the Docker network only, scram-sha-256, distinct least-privilege roles: `app_rw` (no DDL), `migrator` (DDL, used only by the migration job), `readonly` (for you, psql). Sensitive profile fields (`tfn`, `visa_number`, `dob`, `address`) live in a separate `profile_sensitive` table that the LLM code path has **no grant on** — enforced by Postgres, not by developer discipline. |
| Encryption at rest | VM disk on an LUKS-encrypted Proxmox storage if the mini PC is physically accessible; otherwise accept host-level. Backups encrypted (`vzdump` + age, or PBS with encryption key). |
| Browser sandbox | Applier container: non-root UID, `cap_drop: [ALL]`, `security_opt: no-new-privileges`, read-only root fs with tmpfs `/tmp` and `/dev/shm` (2 GB — Chromium crashes on the 64 MB default), no access to the secrets volume, no DB write grants beyond its own tables. Persistent browser profiles stored per-employer in an encrypted volume, never shared, and cleared on `LOGOUT`/expiry. |
| Least privilege | Each worker container gets only the DB role and secrets it needs. Discovery has no submit path. Notifier has no profile read. |
| Logging | Structured JSON (`structlog`), PII redaction filter on `email`, `phone`, `address`, `tfn`, `visa`, webhook URLs, `Authorization` headers, and any `sk-`/`ant-` prefixed token. 30-day retention, `logrotate`. |
| Audit | `events` table is append-only (revoked UPDATE/DELETE for `app_rw`, enforced by grant). |
| Backups | Nightly vzdump + a nightly `pg_dump` to a separate encrypted file, plus your `config/` and `profile/` in a **private** git repo. Test a restore once — a backup you haven't restored is a hypothesis. |

---

## 9. AI architecture

### Task-to-model routing

| Task | Where | Why |
|---|---|---|
| Salary parse, seniority regex, location, clearance keyword hit | **No LLM** | Deterministic, free, instant, testable |
| Work-rights clause classification | **Local (Ollama, Qwen3-8B or Llama-3.1-8B)** | High volume, short context, the classification is easy; keeps ambiguous employment prose off third-party servers |
| Structured requirement extraction | **Local first, cloud fallback on low confidence** | ~30–80 jobs/day × 2k tokens is real money at cloud rates, and trivial locally |
| Match rationale (the "why it matches" text) | **Cloud (Claude Sonnet)** | Quality visibly matters; input is a public job ad + your resume-equivalent facts |
| Cover letter drafting | **Cloud (Claude Sonnet/Opus)** | This is the one place quality directly converts to interviews. An 8B local model writes cover letters that read like an 8B local model. |
| Application free-text answers | **Cloud**, with strict provenance validation | Same |

### PII minimisation

The key realisation: **the job ad is public, and the profile facts that go into a cover
letter are the same facts you're about to send the employer anyway.** So the incremental
exposure from cloud drafting is small — *provided* you never send the fields the employer
doesn't get either.

Enforced by construction:

- The LLM receives a `PublicProfile` pydantic model built by projection from the full
  profile. Fields like `address`, `dob`, `phone`, `tfn`, `visa_grant_number`,
  `passport`, `referee_contacts`, `salary_expectations_floor` are **not on that model**,
  so they cannot be serialised into a prompt. This is a type-system guarantee, not a
  code-review guarantee.
- Contact details are template-filled locally into the PDF after generation.
- Work-rights status is passed as an **enum** (`CITIZEN` / `PR` / `TEMP_GRADUATE_485` /
  `STUDENT_500` / `REQUIRES_SPONSORSHIP`), never as narrative or document numbers.
- Every outbound LLM call is logged with a token count and a hash of the payload, so
  you can audit what left the network.
- If you want zero cloud exposure: set `ai.mode: local_only`. Quality drops noticeably
  on cover letters; everything else is unaffected.

### Anti-fabrication (the hard requirement)

Prompting alone is not sufficient. Three mechanisms:

1. **Provenance IDs.** Every fact in the profile has a stable ID (`exp.internX.b3`,
   `proj.raytracer.tech`, `edu.unsw.degree`). Generation prompts require output as
   structured JSON where each paragraph/bullet carries `source_ids: [...]`.
2. **Provenance validator.** A post-generation check that (a) every `source_id` exists,
   (b) every named entity in the text (company names, technologies, numbers, dates,
   certifications) appears in the union of the cited facts or the job ad. Anything else
   → `FABRICATION_SUSPECTED`, the draft is quarantined, and you get a Discord alert. No
   fallback, no retry-until-it-passes.
3. **Unknown-question detection.** For application questions, the answerer must first
   emit `can_answer: true|false` with the supporting `source_ids`. `false` → the question
   is written to `open_questions` and surfaced to you; the application cannot be
   submitted while any required question is unanswered. This is the mechanism behind
   "if information is missing, stop and ask me."

Deterministic scores are never produced by an LLM. The LLM can adjust a score by at
most ±10 and must justify the adjustment with evidence rows; the base score is
reproducible from the DB alone.

---

## 10. Deduplication and failure semantics

### The key ladder

Postings resolve to a `job_group`. Applications attach to the **group**, never the posting.

1. `(source_id, source_job_id)` — unique index. Same source, same ID → same posting.
2. `(ats_platform, ats_tenant, ats_job_id)` — **the highest-value key.** A SEEK alert
   email and a Greenhouse API record for the same req share this once the SEEK URL is
   resolved. This is what actually collapses cross-source duplicates.
3. `canonical_url` — normalised: lowercase host, strip `utm_*`/`gh_src`/`ref`/session
   params, strip trailing slash, resolve one redirect hop.
4. `(company_id, normalised_title, location_class)` where `company_id` comes from an
   alias table ("Commonwealth Bank" = "CommBank" = "CBA").
5. `simhash(description)` Hamming distance ≤ 3, **or** `pg_trgm similarity(title) > 0.85
   AND same company AND same location_class` — for repostings with light edits.

Matches at layers 4–5 that aren't confirmed by 1–3 are marked `probable_duplicate` and
merged, but the merge is recorded and reversible from the dashboard.

### Failure semantics

| Scenario | Behaviour |
|---|---|
| Job URL changes | Layer 2 or 5 re-matches to the existing group; a `posting_url_changed` event is logged. No new application. |
| Same job on multiple sites | One group, one application. Sources listed on the detail page. |
| Job reposted after closing | Same group. Policy `reapply_policy: never` (default) / `after_days: N`. If never, dashboard shows "already applied 2026-03-14" rather than silently dropping it. |
| Description edited | New `posting_version` row; group unchanged. If a **hard-gate field** changed (work rights, clearance, seniority), the group is re-screened and you're notified. |
| Application partially completes | `application_attempts.state` machine: `PREPARED → NAVIGATING → FILLING → REVIEW_PENDING → SUBMIT_INTENT → SUBMITTED → CONFIRMED`. Any crash leaves the last committed state; resume is only permitted from states before `SUBMIT_INTENT`. |
| **Browser crashes after submit, before confirmation** | This is the dangerous one. Mitigation: immediately before the click, a `SUBMIT_INTENT` row is written and **committed on a dedicated connection** with the pre-submit screenshot and form hash. On restart, `SUBMIT_INTENT` without `SUBMITTED` is **never auto-retried** — it becomes `VERIFY_REQUIRED`, you get a Discord alert, and the system checks the ingestion mailbox for a confirmation email from that employer over the next 24h to auto-resolve it. |
| Discord unavailable | Outbox backs off; pipeline unaffected. |
| Source unavailable | Per-source circuit breaker: 3 consecutive failures → open for 30 min, half-open probe. Other sources continue. `SYSTEM_ERROR` notification once per open, not per failure. |
| LLM API fails | Retry 3× with jitter, then fall back local, then park the job in `ENRICH_PENDING` and retry next cycle. Never generates a document from partial analysis. |
| Internet down | Everything parks; APScheduler `coalesce=True` means you get one catch-up run, not 12. |
| Proxmox reboot | `onboot=1`, Docker `restart: unless-stopped`, APScheduler jobstore in Postgres with `misfire_grace_time=900`. |

---

## 11. Database schema (core)

```
sources ──< raw_postings ──> postings >──┐
                                          ├──> job_groups ──< job_group_postings
companies ──< company_aliases             │        │
    │                                     │        ├──< job_analyses (LLM extraction, versioned)
    └──< company_rules                    │        ├──< job_scores ──< score_features
                                          │        ├──< work_right_assessments
                                          │        ├──< clearance_assessments
                                          │        └──< applications
                                          │                 │
profile_versions ──< profile_facts        │                 ├──< application_attempts ──< attempt_artifacts
    └──< profile_sensitive (separate grant)│                 ├──< generated_documents (resume/cover PDF + source_ids)
                                          │                 ├──< application_answers ──< open_questions
                                          │                 └──< application_status_history
work_items (queue)   events (append-only)   notifications (outbox)   settings   application_rules
```

Selected columns worth calling out:

- `postings`: `posted_at TIMESTAMPTZ`, `posted_at_confidence`, `posted_at_raw TEXT`,
  `first_seen_at`, `closes_at`, `raw_id FK`, `content_simhash BIGINT`,
  `ats_platform`, `ats_tenant`, `ats_job_id`, `canonical_url`, `url_hash` (unique).
- `applications`: unique index on `(job_group_id) WHERE status NOT IN ('WITHDRAWN','FAILED_PERMANENT')`
  — **the database itself makes double-application impossible**, not the application code.
- `application_attempts`: `state`, `submit_intent_at`, `submitted_at`,
  `confirmation_ref`, `form_hash`, `trace_path`.
- `events`: `(entity_type, entity_id, at, actor, action, detail JSONB)`, append-only.
  This is what produces the 14:00→14:07 timeline you described, verbatim.
- `score_features`: `(score_id, feature, weight, value, contribution, evidence TEXT)` —
  so the dashboard and the Discord card render the explanation from data, not from prose.

---

## 12. Estimated monthly cost (AUD)

Assuming you own the mini PC. Sydney electricity ~$0.33/kWh.

| Item | Cheapest | Recommended | Premium |
|---|---|---|---|
| Electricity (delta vs idle: ~10 W avg → ~7 kWh/mo) | $2.40 | $2.40 | $4.00 (local LLM load) |
| LLM API | $0 (local only) | $8–15 (Claude Sonnet, ~60 jobs/day enriched, ~25 letters/wk) | $30–55 (Opus for letters, cloud enrichment) |
| Adzuna API | $0 (free tier) | $0 | negotiated |
| Discord | $0 | $0 | $0 |
| Tailscale | $0 | $0 | $0 |
| Email (IMAP ingestion) | $0 (existing mailbox / alias) | $0 | ~$3 (Fastmail alias domain) |
| Domain | $0 (`.ts.net`) | $0 | ~$2 (au domain, amortised) |
| Backup storage | $0 (existing disk) | $0 | ~$3 (Backblaze B2 offsite) |
| **Total** | **~$2.40/mo** | **~$11–18/mo** | **~$42–67/mo** |

The recommended configuration is the right one. The cheapest gives you noticeably weaker
cover letters, which is the only output that actually converts.

---

## 13. Major risks and limitations

| Risk | Severity | Mitigation |
|---|---|---|
| Coverage gap: the largest AU board (SEEK) is API-inaccessible | High | ATS-first strategy + SEEK's own email alerts. Accept that some SEEK-exclusive recruiter ads become manual-apply cards. |
| ATS adapter rot — Workday/PageUp change their form DOM | Medium | Adapters are small, versioned, and covered by a nightly smoke test against a captured fixture; failures degrade to DRAFT_ONLY, never to a wrong submission. |
| LLM-generated letters read as LLM-generated | Medium-High | Provenance forces concrete, specific claims; style config bans the usual tells; you review every one in Mode 2. Treat the draft as a first draft. |
| Reputational risk from volume | Medium | Mode 2 default, per-company rate limit (`max_applications_per_company_per_month`), global daily cap (default 8). |
| Personal account termination if you ever point the browser at SEEK/LinkedIn | High | The browser layer has a domain allowlist; boards are on the deny list at the code level, not the config level. |
| Ghost jobs / recruiter farming | Medium | Prefer tier-1 direct-employer sources; flag postings with no `ats_job_id` and a recruitment-agency company classification. |
| Mis-parsed work rights causing a wrong application | Medium | `INCOMPATIBLE` is the only auto-block; everything else surfaces the clause verbatim in the card so you see the actual words. |
| Single point of failure (one mini PC) | Low-Medium | Nightly backups + a documented 20-minute restore. The system being down for a day costs you a day of alerts, not a job. |
| You stop reading the Discord channel | Medium, genuinely | Daily summary + deadline nags + `SYSTEM_OFFLINE` dead-man's switch. |

---

## 14. Implementation roadmap

Reordered from your 17-phase list. The changes: security is not phase 14 (it's built into
1, 2 and 3 and audited at the end), the dashboard moves earlier (you need to *see* the
pipeline to debug it), and browser automation moves late because it's the riskiest and
depends on everything else being trustworthy.

| # | Phase | Deliverable | Est. |
|---|---|---|---|
| **1** | **Proxmox foundation** | Hardened Debian VM, Docker, Tailscale, backups, snapshot. Verified reboot survival. | 1 evening |
| 2 | Repo + Docker Compose skeleton | Project layout, compose, Caddy+auth, healthchecks, secrets, CI-less test harness | 1 evening |
| 3 | Database + migrations | Full schema, Alembic, roles/grants, seed, `pg_dump` backup job | 1–2 evenings |
| 4 | Master profile system | YAML schema, pydantic models, provenance IDs, loader, validator, `PublicProfile` projection | 2 evenings (mostly you writing content) |
| 5 | Source framework + first adapters | Adapter ABC, circuit breaker, rate limiter, raw→normalised, Greenhouse + Lever + Ashby, company registry | 2 evenings |
| 6 | Scheduler + queue | APScheduler, work_items, worker loop, misfire handling, restart tests | 1 evening |
| 7 | Deduplication | Key ladder, simhash, company aliasing, merge/unmerge, test corpus | 1–2 evenings |
| 8 | AU filtering & classification | Location gazetteer, seniority, salary parser, work rights, clearance — all rule-based, all unit-tested | 2–3 evenings |
| 9 | Dashboard v1 | Overview, job feed, detail, pipeline board, event timeline | 2 evenings |
| 10 | Discord notifications | Outbox, notifier worker, card renderer, rate limiting, failure tests | 1 evening |
| 11 | AI enrichment + scoring | Local model, routing, structured extraction, deterministic scorer + evidence | 2–3 evenings |
| 12 | Document generation | Resume selection engine, cover letter, answers, **provenance validator**, PDF | 3 evenings |
| 13 | Approval workflow | Modes, rule engine, dashboard approve/reject, dry-run enforcement | 1–2 evenings |
| 14 | Application engine (no browser) | State machine, idempotency, attempt lifecycle, artifact storage, mock adapter | 2 evenings |
| 15 | Browser automation | Playwright harness, mock form suite, Greenhouse/Lever/Ashby adapters, submit-intent protocol | 3–4 evenings |
| 16 | Email-alert ingestion | IMAP, SEEK/LinkedIn/APS alert parsers, tier-1 resolution, manual-apply cards | 2 evenings |
| 17 | Adzuna + secondary sources | Rate-budgeted adapter, predicted-salary handling | 1 evening |
| 18 | Monitoring & resilience | Healthchecks, dead-man's switch, chaos tests (kill workers mid-run), log rotation | 1–2 evenings |
| 19 | Security audit + go-live | Grant review, secret scan, restore test, DRY_RUN off for one company, first real application | 1 evening |

Phases 1–10 give you a working, useful system (real-time Discord alerts for genuinely
matched Sydney grad roles, with a dashboard) with **no AI and no automation** — that
alone is worth having, and it's the foundation everything else sits on.

`DRY_RUN=true` is a global environment variable, read once at startup, checked inside
the submit function itself as an assertion (not just at the caller). It stays `true`
until phase 19.

---

## 15. Repository layout

```
job-agent/
├── compose.yaml                 # base
├── compose.override.yaml        # local dev (bind mounts, DRY_RUN)
├── compose.prod.yaml
├── .env.example                 # never .env
├── Makefile                     # make up / test / migrate / logs / backup
├── README.md
├── docs/
│   ├── 00-architecture.md
│   ├── phases/                  # this document set
│   └── adr/                     # decision records: why Postgres-queue, why HTMX...
├── config/
│   ├── criteria.yaml            # location, seniority, salary, work rights policy
│   ├── sources.yaml             # adapters + automation_policy per source
│   ├── companies.yaml           # ATS registry: company → (platform, tenant)
│   ├── rules.yaml               # auto-apply rules, blacklists, caps
│   └── notifications.yaml
├── profile/                     # YOUR DATA — private repo or SOPS-encrypted
│   ├── identity.yaml            # non-sensitive
│   ├── sensitive.yaml           # SOPS-encrypted; never leaves the VM
│   ├── education.yaml
│   ├── experience.yaml
│   ├── projects.yaml
│   ├── skills.yaml
│   ├── work_rights.yaml
│   ├── preferences.yaml
│   ├── answers/                 # long-form: STAR stories, "why us" fragments
│   │   ├── star.yaml
│   │   └── narratives/*.md
│   └── resumes/                 # master + variants (source docs)
├── src/job_agent/
│   ├── core/                    # config, db, logging, security, clock (tz-aware)
│   ├── models/                  # SQLAlchemy + pydantic
│   ├── profile/                 # loader, validator, PublicProfile projection
│   ├── sources/
│   │   ├── base.py              # SourceAdapter ABC
│   │   ├── ats/                 # greenhouse.py, lever.py, ashby.py, workday.py...
│   │   ├── feeds/               # rss, adzuna
│   │   └── email/               # imap ingestion + per-sender parsers
│   ├── pipeline/                # normalise, dedupe, screen, enrich, score, route
│   ├── screening/               # location, seniority, salary, work_rights, clearance
│   ├── ai/                      # router, local, cloud, prompts/, provenance.py
│   ├── generation/              # resume.py, cover_letter.py, answers.py, pdf.py
│   ├── application/             # state machine, idempotency, attempts
│   ├── automation/              # playwright harness, adapters/, allowlist.py
│   ├── notifications/           # outbox, discord, cards.py
│   ├── scheduler/
│   ├── workers/                 # discovery, pipeline, applier, notifier entrypoints
│   └── web/                     # FastAPI routes, templates/, static/
├── migrations/                  # alembic
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/                # captured API responses, sample ads
│   └── mock_forms/              # a local Flask app serving fake application forms
└── ops/
    ├── caddy/Caddyfile
    ├── backup/
    └── healthcheck/
```

---

## 16. Master profile format — recommendation

**Multiple YAML files in a git repo, schema-validated by pydantic, loaded into Postgres
as an immutable versioned snapshot. Long prose in adjacent Markdown, referenced by ID.**

Why not the alternatives:

- **Markdown alone** — you can't validate it, you can't attach stable provenance IDs, and
  the generator would be re-parsing prose on every run. Wrong for structured facts.
- **JSON** — no comments (you *will* want to annotate "don't mention this one"), painful
  multi-line strings, and STAR answers are multi-line by nature.
- **Database-primary** — hostile to hand-editing, which is your stated priority, and
  loses git history/diffing. You'd end up building a CRUD UI you don't want to maintain.
- **YAML** — comments, readable multi-line via `|`, anchors for repeated blocks, diffs
  beautifully in git, and pydantic validates it into typed models with clear errors.

The Postgres copy exists so that a generated document can be pinned to the exact profile
version that produced it — if you edit your resume in March, an application from January
still renders identically. `profile_versions` stores the content hash and the parsed
snapshot; `generated_documents.profile_version_id` is a FK.

The single most important design detail: **every atomic fact carries a stable ID.**

```yaml
# profile/experience.yaml
- id: exp.acme
  organisation: Acme Pty Ltd
  title: Software Engineering Intern
  location: Sydney, NSW
  start: 2024-11
  end: 2025-02
  bullets:
    - id: exp.acme.b1
      text: Built an internal service that reduced deploy time from 25 to 6 minutes
      tags: [python, ci, docker]
      metrics: {before_minutes: 25, after_minutes: 6}
    - id: exp.acme.b2
      text: Wrote integration tests raising coverage on the billing module to 84%
      tags: [testing, pytest]
```

Cover letter generation must cite `exp.acme.b1` to say anything about deploy times. If
it produces a sentence about deploy times without that citation, the validator kills it.
That is the entire anti-fabrication guarantee in one mechanism.

Sensitive fields live in `profile/sensitive.yaml`, SOPS-encrypted with age, loaded into
`profile_sensitive` under a Postgres role the AI code path has no `SELECT` grant on.

---

## 17. Scheduling, exactly

```python
# src/job_agent/scheduler/main.py  (shape, not final code)
SYDNEY = ZoneInfo("Australia/Sydney")

scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=DB_URL, tablename="apscheduler_jobs")},
    job_defaults={
        "coalesce": True,          # 12 missed runs after an outage → ONE catch-up run
        "max_instances": 1,        # never overlap a source poll with itself
        "misfire_grace_time": 900, # run if <15 min late, skip if later
    },
    timezone=SYDNEY,
)

# Per-source cadence, not one global tick — respects each source's rate budget.
scheduler.add_job(poll_source, CronTrigger(minute="*/30", timezone=SYDNEY),
                  args=["greenhouse"], id="poll:greenhouse", jitter=90)
scheduler.add_job(poll_source, CronTrigger(minute="7,37", timezone=SYDNEY),
                  args=["lever"], id="poll:lever", jitter=90)
scheduler.add_job(poll_source, CronTrigger(hour="*/6", minute=13, timezone=SYDNEY),
                  args=["adzuna"], id="poll:adzuna")          # free-tier budget
scheduler.add_job(poll_source, CronTrigger(minute="*/15", timezone=SYDNEY),
                  args=["imap"], id="poll:imap")
scheduler.add_job(daily_summary, CronTrigger(hour=19, minute=0, timezone=SYDNEY))
```

Points that matter:

- **The scheduler only enqueues.** It writes a `work_item` and returns in milliseconds.
  Workers do the actual fetching. So a slow source can never delay the next tick.
- `jitter` staggers sources so you don't fire eight HTTP clients on the same second.
- All timestamps stored as `TIMESTAMPTZ` in UTC; the timezone is applied at the display
  and scheduling boundaries only. `Australia/Sydney` handles the AEDT/AEST transition
  correctly, including the duplicated 2am hour in April — which is exactly why you use
  a named zone and never a fixed `+10:00` offset.
- The jobstore is in Postgres, so a container restart resumes the schedule rather than
  resetting it.

---

*Next: `PHASE-01-proxmox-foundation.md`. Build phase 1, verify every check in its
completion list, then we move to phase 2.*
