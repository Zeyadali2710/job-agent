# Session handoff — Phases 11→15

State at the end of this session, what changed, and the things a future session
would otherwise have to rediscover the hard way.

- **41 commits**, `ac77f95` → `c153ea4`, all on `claude-code-work` and pushed.
- **Tests 264 → 520.** In the image: 519 passed, 1 skipped (by design, see §6).
- **Migrations at `0013`.** 0010–0013 are new.
- **8 services healthy** on CT 120: postgres, api, worker, scheduler, notifier,
  renderer, applier, caddy.

---

## 1. Operational state — read this first

| Setting | Value | Meaning |
|---|---|---|
| `dry_run` | `true` | Unchanged. Nothing has ever been submitted. |
| `ai.mode` | **`"off"`** | **Paused mid-session and never turned back on.** |
| `automation.adapter` | `"mock"` | The browser adapter exists but is not selected. |
| `generate.max_drafts_per_run` | `2` | Was 5. |
| `generate.min_score` | `45` | |
| `score.weights` | `{"skill_overlap": 35, "seniority_match": 14, "location_rank": 11, "freshness": 8, "work_rights_uncertain": -5}` | Overrides merged over `DEFAULT_WEIGHTS`. |

**Spend: 0.7632 AUD of the 7 AUD monthly budget.** 76 Haiku extractions
(0.3613), 8 Sonnet cover letters (0.2079), 7 Sonnet answer sets (0.1940).

**Six applications sit at `AWAITING_APPROVAL`** at `/applications`. Two test
approvals during the session were reverted; every decision is still yours.

To resume AI work: `UPDATE app.settings SET value='"on"'::jsonb WHERE key='ai.mode';`

### Why `ai.mode` was turned off

Phase 12 added `draft` and `generate` to the scheduler **hourly**. Each drafted
application is a Sonnet cover letter plus a Sonnet answer set — 0.054 AUD — so
hourly × 5 drafts is ~6.5 AUD/day and would have consumed the monthly cap in a
day. It drafted 6 applications and made 15 Sonnet calls before this was caught.

Fixed, but the fix is untested at cadence because AI has been off since:
drafting is now **daily at 07:25 Sydney** with `max_drafts_per_run: 2`
(~3.2 AUD/month). The cron cadence and that number are one decision — changing
either alone changes the bill. `submit` is deliberately **not** on a cron: a
DRY_RUN rehearsal returns the application to APPROVED, so a scheduled submit
would rehearse the same application forever. Approval enqueues it instead.

---

## 2. What was built

### Phase 11 — enrichment and scoring (completed early in the session)
Haiku extraction of skills/requirements, a deterministic scorer, and the
`app.current_job_scores` view. Then a long tuning pass, summarised in §4.

### Phase 12 — document generation
`generation/` — deterministic resume selection, Sonnet cover letters, the
provenance validator, answers, and PDF rendering via **fpdf2** (a pure-Python
wheel; WeasyPrint would have put pango/cairo into the image every service
builds from).

**The split is the security design, not an implementation detail.** The
*worker* drafts: it holds the API key and only ever sees facts that survived
deterministic selection. The *renderer* renders: it is the only role with
`USAGE` on schema `sensitive`, the only service on `internal` alone, and the
only thing that template-fills contact details. Verified on the box — renderer
reads 14 sensitive rows, worker is refused, renderer cannot resolve
`api.anthropic.com`.

The resume is assembled from fact bodies copied verbatim, so it is exempt from
the entity scan by construction. The cover letter is guilty until the validator
clears it; a failure quarantines the draft, raises
`MANUAL_INTERVENTION_REQUIRED`, and is **never rendered to PDF**, so a
quarantined letter cannot sit in the artifacts directory looking like a good one.
There is no regeneration loop — architecture §9 says quarantine, not retry.

### Phase 13 — approval workflow
`application/rules.py` (routing), `application/transitions.py` (the status
transition table the schema was missing — `applications.status` had a CHECK on
the *values* but nothing said which may follow which), the Applications tab,
and the `APPROVAL_REQUIRED` card.

Dry-run enforcement is asserted as a property, not claimed: tests check that
nothing on the approval path writes `application_attempts` or holds an HTTP
client, and read the dashboard handler's decision map **out of the AST** to
prove it can only reach APPROVED, WITHDRAWN or AWAITING_APPROVAL.

### Phase 14 — application engine
Attempt lifecycle, idempotency, artifact storage, mock adapter.

**All three DRY_RUN layers are now real**, and each was tested:
1. the environment variable (was already there);
2. `assert_submission_allowed()` — added this phase, inside the submit path so
   a new caller inherits it. It **raises rather than asserting**, because
   `python -O` strips assert statements;
3. the database trigger, verified directly:
   `ERROR: DRY_RUN is active: refusing to record a live submission attempt`.

The `SUBMIT_INTENT` row is committed in its own transaction *before* the click.
Nothing at or past that state is ever auto-retried — it resolves to
`VERIFY_REQUIRED`, because a retry there is how one application becomes two in
a recruiter's inbox. Orphan resolution runs at applier **startup** as well as on
a cron; the moment to notice what the last crash left is startup.

### Phase 15 — browser automation
`automation/` + `Dockerfile.browser`. Chromium lives in the applier's image
alone. The applier gained `edge` (a browser that cannot load a form cannot fill
one); the **renderer stays on `internal` only**, because it is the one that
reads contact details.

The browser sits behind a `PageDriver` protocol, so every decision — refuse or
proceed, which selector gets which value, whether a plan may go forward — is
tested against real captured pages **without Chromium**. A safety property that
can only be exercised by starting a browser is one that gets tested once and
then trusted.

**Not built, deliberately: the live click.** `submit()` raises rather than
guessing. Knowing what a successful submission looks like requires one
supervised real submission — Phase 19's job.

---

## 3. Things only real evidence revealed

Every consequential bug this session came from checking against reality. This
section is the argument for keeping that habit.

**My CAPTCHA rule would have refused 100% of real application forms.** The first
version refused any page mentioning recaptcha/hcaptcha. Fetching three real
forms showed all of them load *invisible, score-based* captcha —
Greenhouse's `GOOGLE_RECAPTCHA_INVISIBLE_KEY`, Ashby's hidden v3 badge, Lever's
hCaptcha whose `onSuccess` clicks submit itself. The rule looked correctly
cautious and every test written against invented pages passed. The line is now
at a challenge being *presented*, not at background fraud scoring.

**Greenhouse ships honeypots.** Two inputs that are `required`, `aria-hidden`,
`tabindex="-1"` and unlabelled. An adapter filling every required field would
fill them and announce itself as a bot on every application.

**`g-recaptcha-response` is a plain `<textarea>`** (Ashby); Lever exposes
`h-captcha-response` the same way. To an adapter looking for somewhere to put an
answer these look exactly like a question. Writing to one forges a captcha result.

**Ashby serves no `<form>` and no `<input>` at all** — not in the served HTML,
and not even once rendered; the fields sit loose in the DOM. This is the
concrete reason a browser is required rather than an HTTP client.

**The Ashby form asks work rights as a radio group** whose options include
*"Graduate visa (e.g 485)"* — the applicant's actual situation. No choice field
is ever answered automatically.

**A US clearance is not an AGSVA clearance.** Turning on the 485 profile
screened out 1,056 groups — 26% of the corpus — as
`CLEARANCE_CITIZENSHIP_CONFLICT`. Every sampled one was an American defence
listing wanting a "U.S. Secret security clearance", already screened out for
location. Nothing was lost, but the reason was false. Now 91.

**The first cover letter merged two engagements**: "as Product Owner for a
five-person capstone team, I directed the integration of five codebases" —
every entity real, both numbers cited, and it fused a five-person university
capstone with a three-person client contract. Adding a rule to the prompt did
not stop it. Entity checking cannot see relations between facts, so
`MERGED_SOURCES` is an **advisory** that names which engagements a paragraph
spans; a hard failure would reject most good drafts.

---

## 4. Scoring: what the tuning actually found

Normalising skill vocabulary (`PostgreSQL` vs `Postgres`) was **investigated and
rejected on evidence**: one near-miss in forty rows of extracted skills. Don't
rebuild it without new data.

The screen homogenises the survivors, so most features carry no ranking
information. What matters is spread × weight:

| feature | spread | old weight | ranking power |
|---|---|---|---|
| seniority_match | 0.154 | 28 | 4.31 — but 90% sit at 0.5 |
| skill_overlap | 0.179 | 18 | 3.22 — the only field-aware feature |
| grad_program | 0.225 | 10 | 2.25 — measures title wording, like seniority |
| location_rank | 0.116 | 18 | 2.09 — everything already passed the gate |

`seniority_match` and `grad_program` were spending 38 points double-counting
title wording, which is why three internships outranked both security roles.
Weight moved into `skill_overlap`; the security roles now sit at #3 and #5 and
`2026 Mechanical Engineering Intern` (0 of 10 skills matched) fell from #3 to #7.

**Known ceiling:** `skill_overlap` maxes at **0.63** across the corpus, so ~13 of
its 35 points are unreachable. Nothing scores above ~59, and
`notify.high_match_min_score` is still 80 — **`HIGH_MATCH_JOB` can never fire.**
Lower the threshold or accept that Discord match alerts are dormant.

---

## 5. Deploying — CT 120 cannot `git pull`

`git pull` on CT 120 fails non-interactively:
`could not read Username for 'https://github.com'`. There is no credential
helper. **This silently deploys old code** — a build will succeed against an
unchanged working tree and everything looks fine.

Workaround used all session, a real fast-forward with identical object ids, so a
later `git pull` from GitHub still works:

```bash
REMOTE_SHA=$(ssh zeyad@job-agent.tailf137cc.ts.net 'cd ~/job-agent && git rev-parse HEAD')
git bundle create /tmp/ja.bundle ^$REMOTE_SHA claude-code-work
cat /tmp/ja.bundle | ssh zeyad@job-agent.tailf137cc.ts.net \
  'cat > /tmp/ja.bundle && cd ~/job-agent && git fetch -q /tmp/ja.bundle claude-code-work \
   && git merge --ff-only -q FETCH_HEAD && rm -f /tmp/ja.bundle'
```

**Worth fixing properly** with a deploy key or a credential helper.

### Other deployment traps found the hard way

- **`make build` does not deploy.** It updates images; the running containers
  keep the old one. `make up` is now the deploy verb (`--profile tools build`
  then `up -d`). A re-screen reproduced the pre-fix numbers exactly before this
  was caught.
- **`make test` cannot detect that.** `docker compose run --rm worker` creates a
  fresh container from the newest image, so tests always run the code you just
  wrote, never the code the long-running worker is executing. 274 passing tests
  told us nothing.
- **`docker compose exec -T` eats stdin.** Inside `ssh … bash -s <<'EOF'` it
  consumes the rest of the script and everything after it silently vanishes.
  Redirect each one: `docker compose exec -T … </dev/null`.
- **Tailscale SSH check mode.** Sessions lapse and a `BatchMode` ssh then
  *hangs ~40s and exits 0* rather than failing. The re-auth link only lives as
  long as the session that issued it, so a short session's link is dead before
  it can be opened. A connection with no `ConnectTimeout` survives long enough.

---

## 6. Traps encoded as tests

`tests/unit/test_sql_hygiene.py` exists because both of these cost a deploy cycle:

- **A bare `:param IS NULL` in a `text()` query** raises asyncpg
  `AmbiguousParameterError` before the query runs; casting only the second use
  does not reach the first. Took down the worker and the renderer together.
- **A colon-prefixed name inside a SQL comment** becomes a *required* bind
  parameter — `text()` regex-scans the whole string and does not parse comments.
  A comment explaining the bug above caused the same class of bug.

Other properties worth not breaking:

- `tests/unit/test_approval_cannot_submit.py` — nothing on the approval path
  writes `application_attempts` or holds an HTTP client.
- `test_playwright_is_not_in_the_shared_requirements` — **skips** in the image.
  `requirements-browser.txt` is deliberately absent from the worker image; only
  `src/`, `tests/` and `requirements.txt` reach it.
- **A grep is not a property.** Three tests in this session failed on their own
  docstrings before being rewritten to read the AST.

---

## 7. Evidence kept

`tests/fixtures/ats/` — four real ATS pages with `PROVENANCE.json` recording the
URL each came from, trimmed from ~900KB of page to ~15KB of evidence and
re-serialised from the parsed DOM (the first attempt sliced tags with a regex
and truncated at 300 chars, producing tags that swallowed the following
content). `ashby_rendered.html` was captured **through Playwright at
networkidle** and is the only place Ashby's form exists.

---

## 8. Open, in the order I'd take them

1. **Turn `ai.mode` back on** when ready. The prompt now carries the citation
   rule that caused most quarantines; worth one regeneration (~0.05 AUD) to see
   the rate drop. Watch that the daily cadence holds.
2. **Phase 16 — email ingestion.** Needs IMAP host/address/app password, and a
   few real alert emails to read before writing parsers.
3. **Phase 17 — Adzuna.** Needs an app ID and key from developer.adzuna.com.
   Both go in `/opt/job-agent/secrets/` with `chgrp 10001` + `chmod 640`
   (**needs sudo**; the containers run as UID/GID 10001 and this caused about
   six separate failures during the original build).
4. **Phase 18 — monitoring and resilience.** Unblocked.
5. **`notify.high_match_min_score`** — currently unreachable, see §4.
6. **The live click** — Phase 19, and it needs one supervised submission.

## 9. Deliberate decisions, so they are not "fixed" later

- **No retry on a provenance failure.** Retrying until something passes turns a
  safety property into a sampling exercise.
- **No choice field is ever answered automatically** on an application form.
- **`MERGED_SOURCES` is advisory**, never a quarantine.
- **The mock adapter is the default.** A browser loading an employer's careers
  page is a visible act and should be deliberate.
- **Nothing in `config/questions.yaml` is `required`.** Marking a speculative
  placeholder required held every application at DRAFT forever — "why do you
  want to work here?" is not answerable from profile facts, so the gate could
  never clear. Real required questions arrive from the form in Phase 15.
- **The applier reads identity but never schema `sensitive`.** It can type your
  name into a form and cannot read your address, TFN or visa number.
