# Job Agent — Revised Architecture for GMKtec M8

**Supersedes `00-ARCHITECTURE.md` §3 (Proxmox architecture), §9 (AI), §12 (cost), and
`PHASE-01-proxmox-foundation.md` in its entirety.**
Everything else in the original document stands — see §9 (Migration) for the exact list.

Status: **awaiting your approval.** No build commands here by design.

---

## 1. Your hardware, confirmed

The GMKtec M8 is built on the AMD Ryzen 5 PRO 6650H — a 6-core, 12-thread Zen 3+ part
on a 6 nm process, boosting to 4.5 GHz, with a Radeon 660M iGPU and LPDDR5-6400 memory.
Reviewers note the memory is soldered to the board, so 16 GB is the maximum configuration
available, while the two M.2 2280 slots let you upgrade the 512 GB SSD or add a second drive.
BIOS exposes three power modes — Quiet 28 W, Balance 35 W, Performance 40 W — plus VRAM
allocation and an Auto Power On option.

Four consequences that drive every decision below:

| Fact | Consequence |
|---|---|
| **16 GB soldered — permanent ceiling, no upgrade path** | The architecture has to be right-sized *forever*, not "for now". Conservative browser concurrency isn't a starting point you graduate from; it's the permanent design. |
| **Second M.2 slot is free** | Storage is your *only* upgrade path. A second NVMe is the single highest-value $80 you can spend on this box, and it solves the backup problem properly. |
| **6C/12T Zen 3+, no usable GPU compute** | Plenty for this workload. Nowhere near enough for a local LLM (§6). |
| **BIOS has Auto Power On** | Must be enabled. Without it, a power blip means the homelab stays dark until you notice. |

**Confirm before we build** (read-only, run on the PVE host):

```
lscpu | head -20
free -h
pvesm status
lsblk && vgs && lvs
pveversion
cat /proc/swaps
zpool list 2>/dev/null || echo "not ZFS"
pct config 114 ; pct config <crafty-id>
```

The last one matters: I need Studio-lab's real VMID and whether your PVE root is ext4/LVM
or ZFS. ZFS changes the RAM budget by about 1 GB (ARC).

---

## 2. Recommended Proxmox layout

```
GMKtec M8 — Ryzen 5 PRO 6650H (6C/12T) · 16 GB LPDDR5 (soldered) · 512 GB NVMe
│
├── Proxmox VE host                                    ~1.0 GiB RAM · ~15 GiB disk
│   ├── PVE firewall (per-CT rules)
│   ├── vzdump schedule → local + off-box target
│   └── watchdog cron (dead-man's switch → Discord)
│
├── CT 114  studio-lab           [RUNNING]   6 cores · 6 GiB (was 9) · 78 GiB
│
├── CT 120  job-agent            [NEW]       6 cores · 7 GiB · 2 GiB swap · 60 GiB
│   │                                        unprivileged · nesting=1 · keyctl=1
│   │                                        static 192.168.1.115
│   └── Docker Compose
│       ├── postgres      1.0 GiB · 1.5 cpu   ← the only datastore
│       ├── api           512 MiB · 1.0 cpu   ← FastAPI + HTMX dashboard
│       ├── scheduler     256 MiB · 0.5 cpu   ← APScheduler, enqueue-only
│       ├── worker        768 MiB · 2.0 cpu   ← discovery, pipeline, generation
│       ├── browser       2.5 GiB · 2.0 cpu   ← Playwright/Chromium, MAX_CONCURRENT=1
│       ├── notifier      192 MiB · 0.5 cpu   ← Discord outbox drain
│       └── caddy         128 MiB · 0.5 cpu   ← TLS + auth, binds LAN + tailscale0
│
├── CT ???  crafty-controller    [STOPPED]   2 cores · 5.59 GiB · 32 GiB
│                                            ⚠ reduce to 3 GiB before ever starting
│
└── (recommended) second M.2 NVMe → PVE directory storage `backup-nvme`
```

**No VM anywhere.** That is the single biggest change from the previous guide, and §8
explains why the hardware reversed my earlier recommendation.

---

## 3. RAM budget — the calculation

### 3.1 What's actually available

| Line | GiB | Note |
|---|---:|---|
| Physical LPDDR5 | 16.00 | soldered, final |
| − iGPU UMA reserve | −0.50 | BIOS default may be 2 GB on "Auto" — **set to minimum**, this box is headless |
| = Visible to Proxmox | ~15.50 | verify with `free -h` |
| − PVE host (kernel, pvedaemon, pvestatd, rrdcached, pve-firewall) | −0.90 | measured typical for a single node, no cluster |
| − ZFS ARC **if** your root is ZFS | −1.00 | cap it: `options zfs zfs_arc_max=1073741824`. **0 if ext4/LVM** |
| **= Available to guests** | **~14.60** | (13.60 on ZFS) |

If `free -h` shows ~13.5 GiB rather than ~15.5, your BIOS is handing 2 GB to the iGPU.
Fixing that is worth more than any software tuning in this document.

### 3.2 The critical LXC memory fact

**An LXC's `memory` setting is a cgroup ceiling, not a reservation.** Studio-lab's 9 GiB
allocation is not consuming 9 GiB — it is consuming the 676 MiB it actually uses, and the
other 8.3 GiB is genuinely free for everything else on the box right now.

This is the opposite of a VM, where allocated RAM is committed to the guest whether it's
used or not. It's also why lowering an LXC's limit doesn't "free up" memory — it only
lowers the point at which that container gets OOM-killed.

So the question about Studio-lab is not "can we take RAM from it" (there's nothing to
take). It's "are the *ceilings* summed safely, so that two simultaneous spikes can't OOM
the host?"

### 3.3 Allocation

| Component | RAM ceiling | Swap | Steady-state actual |
|---|---:|---:|---:|
| Proxmox host | 0.90 GiB | — | 0.85 GiB |
| ZFS ARC (if applicable) | 1.00 GiB | — | varies |
| CT 114 studio-lab | **6.00 GiB** (from 9) | 2 GiB | 0.66 GiB observed |
| CT 120 job-agent | **7.00 GiB** | 2 GiB | ~1.5 GiB idle / ~5.8 GiB during a browser run |
| CT ??? crafty | 0 (stopped) | — | 0 |
| **Sum of ceilings** | **13.90 GiB** | | |
| **Free even if everything peaks simultaneously** | **0.70 GiB** | | |
| **Free at realistic steady state** | **~12.1 GiB** | | |

The worst case — Studio-lab pinned at its ceiling *and* a browser batch running — leaves
0.7 GiB of host headroom plus 4 GiB of swap. That's tight but survivable, and it is a
scenario that requires Studio-lab to grow 9× beyond anything you've observed.

The realistic case leaves ~12 GiB free, which is why this design has room to breathe.

### 3.4 Inside the job-agent container

7.00 GiB ceiling, allocated as Docker `mem_limit`s:

| Container | Limit | Expected actual | Reasoning |
|---|---:|---:|---|
| LXC OS + dockerd/containerd | 500 MiB | ~380 MiB | Debian minimal + Docker daemon measured ~200 MiB, plus page cache |
| postgres | 1024 MiB | ~450 MiB | `shared_buffers=256MB`, `work_mem=8MB`, `max_connections=40`, `effective_cache_size=768MB`, `maintenance_work_mem=64MB`. Your dataset is <1 GB/year (§5) — this is generous. |
| api | 512 MiB | ~180 MiB | uvicorn, 2 workers, Jinja templates. No SPA build, no Node. |
| scheduler | 256 MiB | ~70 MiB | APScheduler only enqueues; it never fetches |
| worker | 768 MiB | ~250 MiB, ~600 MiB peak | HTTP fetching, HTML parsing, LLM calls, PDF generation (WeasyPrint peaks hardest) |
| browser | **2560 MiB** | ~900 MiB typical, ~1.6 GiB peak | see §4 |
| notifier | 192 MiB | ~60 MiB | |
| caddy | 128 MiB | ~30 MiB | |
| **Sum of limits** | **5.87 GiB** | **~1.4 GiB idle / ~5.5 GiB peak** | |
| Slack under the 7 GiB ceiling | 1.13 GiB | | absorbs page cache and short spikes |

Every limit is enforced by Docker, so a leak in one container cannot take down the
others, and the LXC ceiling means the worst case cannot take down Proxmox.

---

## 4. Browser automation — the RAM calculation you asked for

This is where homelab job-application systems die, so here are real numbers rather than a
hand-wave. Chromium is multi-process; you pay for every process:

| Process | Typical RSS | Notes |
|---|---:|---|
| Browser (main) process | 180–250 MB | |
| GPU process | 120–200 MB | present even headless, using SwiftShader; can be reduced with `--disable-gpu` |
| Network service | 60–110 MB | |
| Storage service + utility processes | 60–120 MB | |
| Renderer, per page | 120–400 MB | site isolation means one per origin |
| Renderer on a heavy SPA (Workday, PageUp) | **400–700 MB** | iframes are separate renderers; Workday forms routinely spawn 3–4 |
| Playwright Node driver | 70–100 MB | |
| **One browser, one simple form** | **~700–950 MB** | Greenhouse, Lever, Ashby |
| **One browser, one Workday form** | **~1.3–1.7 GB** | the realistic worst case |

Plus `/dev/shm`: Chromium uses shared memory heavily and Docker's 64 MB default causes
tab crashes. `shm_size: 1gb` — and note **tmpfs pages count against the container's memory
limit**, so the 2560 MiB limit has to cover both.

**2.5 GiB limit ÷ ~1.7 GiB worst-case browser = MAX_CONCURRENT_BROWSERS: 1.**

Not "start at 1 and raise it later". On a permanently-16 GB box, one is the number. Two
concurrent Workday sessions would be 3.4 GB and would push the container past its limit
mid-submission, which is the single worst moment for a process to die.

### Enforced controls

| Control | Value | Why |
|---|---|---|
| `MAX_CONCURRENT_BROWSERS` | 1 | above |
| `MAX_APPLICATIONS_PER_RUN` | 3 | bounds a batch to ~12 minutes |
| Applications processed | strictly sequentially | queue is `FOR UPDATE SKIP LOCKED` with a worker count of 1 for the browser lane |
| Per-application wall clock | 240 s hard kill | `asyncio.wait_for` around the whole attempt, plus a `SIGKILL` fallback |
| Per-navigation timeout | 30 s | Playwright `default_navigation_timeout` |
| Browser lifetime | one `BrowserContext` per application, browser recycled every 5 applications | prevents renderer leak accumulation |
| PID 1 | `init: true` (tini) | orphaned Chromium children get reaped instead of becoming zombies |
| Reaper | every 5 min, kill any `chrome`/`chromium` process with elapsed > 15 min | belt and braces |
| Docker | `mem_limit: 2560m`, `memswap_limit: 2560m`, `cpus: 2.0`, `pids_limit: 512` | `memswap_limit` equal to `mem_limit` disables swap for this container — a swapping browser is worse than a killed one |
| Restart policy | `unless-stopped`, `restart_delay` 30 s | |
| Disk | browser profile dir on a tmpfs-backed path, wiped per application | no session leakage between employers |

If the browser container hits its memory limit, Docker OOM-kills *only* that container.
The attempt is left at whatever state was last committed, the `SUBMIT_INTENT` protocol
(original doc §10) decides whether it's safe to retry, and you get a Discord alert. The
rest of the system doesn't notice.

---

## 5. Storage plan

512 GB NVMe ≈ **476 GiB usable**. Your exact split depends on the installer defaults —
confirm with `lsblk`/`vgs`/`lvs`. A typical single-disk PVE install on 512 GB gives
roughly:

| Region | Approx. size |
|---|---:|
| EFI + boot | ~1 GiB |
| swap (LV) | ~8 GiB |
| `local` — root ext4 (`/`, `/var/lib/vz`: ISOs, templates, **dumps**) | ~100 GiB |
| `local-lvm` — thin pool for CT/VM disks | ~350 GiB |

### 5.1 Allocation

| Consumer | Allocated | Realistically used | Where |
|---|---:|---:|---|
| PVE root, ISOs, templates | ~100 GiB region | ~12 GiB | `local` |
| vzdump on-box (fast restore) | 40 GiB budget | ~19 GiB | `local` (`/var/lib/vz/dump`) |
| CT 114 studio-lab | 78.19 GiB | unknown — check `pct df 114` | `local-lvm` (thin) |
| CT ??? crafty (stopped) | 32 GiB | ~3 GiB written | `local-lvm` (thin) |
| **CT 120 job-agent** | **60 GiB** | ~20 GiB at 12 months | `local-lvm` (thin) |
| **Thin pool allocated** | **170 GiB of ~350** | **49%** | leaves real headroom |
| Free `local` after backups | ~48 GiB | | |

**Thin pool discipline matters more than raw free space.** An LVM thin pool that fills
completely corrupts the volumes on it. Keep allocation under 70% and alert at 75% —
that's a monitored metric in §7, not a hope.

**Do not** allocate the remaining ~180 GiB of thin pool. It's your margin for a snapshot
before a risky change, for growing `job-agent` online (`pct resize 120 rootfs +20G` —
one command, no downtime, no partition surgery), and for whatever homelab service you
want next year.

### 5.2 Inside the 60 GiB job-agent container

| Item | Budget | Calculation |
|---|---:|---|
| Debian base + apt | 2.5 GiB | |
| Docker images | 8.0 GiB | Playwright's Python image is ~2.0–2.5 GiB on its own; budget for two versions so you can roll back |
| Docker overlay + build cache | 4.0 GiB | pruned weekly |
| PostgreSQL data | 6.0 GiB | 60 discovered/day → 22k/yr. Raw JSONB ~15 KB + normalised ~8 KB, +30% indexes ≈ **660 MB/yr**. 6 GiB covers ~8 years. |
| Application artifacts | 12.0 GiB | ~7 MB per attempt (Playwright trace 3–8 MB, 3 screenshots ~600 KB, resume PDF ~120 KB, cover ~80 KB). 30 apps/wk = 1,560/yr = **11 GiB/yr uncapped**; with retention (traces 30 d on success, 180 d on failure; PDFs kept forever at ~200 KB each = 312 MB/yr) steady state is **~2–3 GiB**. |
| Logs | 3.0 GiB | Docker `max-size 10m, max-file 3` per container ≈ 210 MB, plus app logs rotated at 30 d |
| Local `pg_dump` | 4.0 GiB | nightly custom-format dump ~80 MB compressed × 14 + 8 weekly |
| tmp + browser profiles | 3.0 GiB | tmpfs-backed, wiped per run |
| **Free headroom** | **17.5 GiB (29%)** | |

Retention jobs run nightly and are part of Phase 18, not an afterthought.

---

## 6. AI — verdict: no local LLM on this box

### The arithmetic

The 6650H is a Zen 3+ CPU with a Radeon 660M. ROCm support for that iGPU generation is
unofficial and painful, so realistically it's CPU-only inference. Dual-channel LPDDR5-6400
gives roughly 50–60 GB/s of usable bandwidth in practice.

An 8B model at Q4_K_M is ~4.9 GB of weights. Token generation is memory-bandwidth bound,
so expect roughly **6–9 tokens/second**. A 600-word cover letter is ~800 tokens —
**90 to 130 seconds per letter**, at a quality level that is visibly worse than a cloud
model, on the one output where quality directly converts to interviews.

The cost side is worse than the speed side: it needs **5–6 GB resident**. That is your
entire realistic headroom, and it collides directly with the browser container — the two
things you'd most want running at once.

**Recommendation: remove Ollama and local LLM inference from the architecture entirely.**
This is a clean win, not a compromise. It reclaims ~6 GB of design pressure and roughly
$4/month of electricity for about $6/month of API tokens.

### What stays local (and it's most of the pipeline)

None of this needs a model at all:

- Salary parsing, seniority classification, location gazetteer, clearance keywords,
  work-rights clause matching — regex and rules, microseconds, free, unit-testable
- Deduplication — `pg_trgm` similarity + simhash + the ATS key ladder, all in Postgres
- Scheduling, queueing, state machine, dedup merges, all DB operations
- The hard screen that kills ~80% of jobs before a single token is spent

### What goes to the cloud

| Task | Model class | Volume |
|---|---|---|
| Structured requirement extraction on survivors | Haiku-class | ~12/day (post-screen) |
| Ambiguous work-rights / clearance clauses | Haiku-class | ~3/day |
| Match rationale text | Sonnet-class | ~8/day |
| Cover letters and free-text answers | Sonnet-class | ~30/week |

The PII-minimisation design from the original document (§9) is unchanged and does the
heavy lifting: the `PublicProfile` pydantic projection makes it structurally impossible
to serialise your address, DOB, phone, visa number or referee contacts into a prompt.

### The one local model worth considering — later

A sentence-embedding model (`all-MiniLM-L6-v2` via ONNX, ~90 MB weights, ~250 MB RSS,
~5 ms/document on these cores) would improve semantic dedup and skill matching for
negligible cost. **Deferred.** `pg_trgm` + simhash + the ATS key ladder will very likely
be sufficient; adding this before we've measured dedup accuracy in Phase 7 would be
exactly the over-engineering you asked me to avoid.

### Revised cost (AUD/month)

| Item | Cheapest | **Recommended** | Premium |
|---|---:|---:|---:|
| Electricity — delta attributable to this system (~5 W avg over idle, at $0.33/kWh) | $1.20 | $1.20 | $1.80 |
| LLM API | $3 (Haiku-only, terse letters) | **$9–14** | $30–45 (Opus letters) |
| Adzuna | $0 (free tier) | $0 | — |
| Discord / Tailscale / DNS | $0 | $0 | $0 |
| Second M.2 NVMe (1 TB, amortised over 4 yrs) | $0 (skip) | **$2** | $2 |
| **Total** | **~$4/mo** | **~$12–17/mo** | **~$34–49/mo** |

Set a hard monthly spend cap in the API console *and* a token budget in config that halts
enrichment when exceeded — a retry loop against a paid API is the only way this system can
cost you real money.

---

## 7. Networking, backup, monitoring

### 7.1 Networking

| Decision | Recommendation | Why |
|---|---|---|
| Addressing | **Static in the CT config**: `192.168.1.115/24`, gw `192.168.1.1` — and shrink the router's DHCP pool so `.115` sits outside it | Static-in-container beats a DHCP reservation here: the container is addressable the instant it boots, with no dependency on the router's lease timing, and the address travels inside `vzdump` backups. Studio-lab on `.114` suggests it's in the DHCP range; consider moving infrastructure to a static block below the pool. |
| Bridge | Existing `vmbr0`, untagged, `virtio` | No VLANs needed. The second 2.5 GbE NIC stays unused — it's a nice option for later network segmentation, but adding a second bridge now buys nothing. |
| Hostname | `job-agent`, plus an A record on your router/Pi-hole for `job-agent.lan` | |
| HTTPS | **Tailscale-issued cert** for `job-agent.<tailnet>.ts.net`, served by Caddy | Gives you a genuinely valid certificate with no browser warnings, works on the LAN *and* remotely, and requires nothing exposed publicly. Caddy's internal CA works too but means installing a root CA on every device. |
| Firewall | **Proxmox CT-level firewall**, not `ufw` inside | One less service in the container, and it's enforced outside the guest so a compromised container can't disable it. Rules: IN `tcp/22` from `192.168.1.0/24`; IN `tcp/443` from `192.168.1.0/24`; default IN drop; OUT allow. |
| Remote access | Tailscale, inbound governed by Tailscale ACLs | Note: tailnet traffic arrives on `tailscale0` *inside* the container and bypasses the bridge firewall, so the ACL is the control, not the PVE rule. |
| Public internet inbound | **None.** No port forwards, no Tailscale Funnel | |
| Outbound required | 443 (ATS APIs, Adzuna, LLM, Discord, registries), 993 (IMAP), 123 (NTP), UDP 41641 + 443 (Tailscale) | |

### 7.2 Backup

The honest problem: **a backup on the same 512 GB SSD does not survive that SSD failing**,
and that's the most likely hardware failure on this box.

**Minimum viable off-box target, in order of preference:**

1. **A second M.2 NVMe in the free slot** (~$70–100 AUD for 1 TB), added to PVE as a
   directory storage. Cheapest, fastest, silent, no cables. Doesn't protect against
   theft/fire/PSU-kills-everything, but does protect against the single most likely
   failure. **This is my recommendation.**
2. A USB 3.2 SSD as a directory storage — protects against the same failures, and can be
   unplugged and stored elsewhere.
3. An SMB/NFS share on your desktop or a NAS, if you have one powered most of the time.

Do **not** run Proxmox Backup Server on this node. PBS wants 1–2 GiB of RAM for its
dedup index, and you don't have it to spare. Plain `vzdump` to a directory storage is the
right tool at this scale.

| What | Where | Frequency | Retention | Encryption |
|---|---|---|---|---|
| CT 120 full (`vzdump`, zstd, snapshot mode) | `local` | daily 03:15 | keep 2 | — (local) |
| CT 120 full | off-box target | daily 03:30 | 14 daily / 8 weekly | age-encrypted post-hook |
| CT 114 studio-lab | off-box target | weekly | 4 | — |
| `pg_dump -Fc` | inside CT + off-box | daily 02:30 | 14 / 8 weekly | age |
| `config/` + `profile/` | **private git repo, off-box** | on every edit | full history | SOPS+age for `sensitive.yaml` |

**Excluded from backup** (`--exclude-path`): Docker images and overlay (rebuildable from
compose), build caches, `/tmp`, browser profile directories, Playwright traces older than
the retention window, and **any persisted browser session/cookie store** — those are live
credentials to employer portals and should not exist in a backup archive at all.

Compressed sizes: job-agent at ~20 GiB used → ~7 GiB per archive. 22 archives ≈ 154 GiB,
comfortable on a 500 GB–1 TB second drive.

**Recovery procedure**, which you should rehearse once in Phase 19:

1. Fresh PVE install if needed → `pvesm add dir backup-nvme --path /mnt/backup-nvme`
2. `pcrestore` / `pct restore 120 <archive> --storage local-lvm`
3. Start CT, verify `docker compose ps`, verify Postgres accepts connections
4. If restoring data only: `pg_restore` the latest dump into a running stack
5. Re-clone `config/` and `profile/` from git, `sops -d` the sensitive file
6. Re-authenticate Tailscale, re-place secrets in `/opt/job-agent/secrets`
7. Confirm `DRY_RUN=true` before letting the scheduler run

The irreplaceable data — your profile, your criteria, your application history — is a few
hundred MB at most. Everything else is rebuildable. Design the recovery around that.

### 7.3 Monitoring

**Explicitly rejected:** Prometheus (200–400 MB + growing TSDB), Grafana (150–250 MB),
Netdata (200 MB+), Loki, Elasticsearch. On a 16 GB box, a monitoring stack that costs
500 MB to tell you that you're short on memory is self-defeating.

**What we use instead — total added RAM ≈ 0:**

| Layer | Mechanism | Covers |
|---|---|---|
| Host metrics | Proxmox's built-in RRD graphs, already running | RAM, CPU, disk, network history per CT |
| Host alerting | PVE notification target (8.2+ supports a generic webhook target you can template into Discord's JSON — verify on your version; otherwise a 15-line cron script) | node-level events, backup failures |
| **Dead-man's switch** | Cron on the **PVE host** (not in the container) curls `https://192.168.1.115/health` every 5 min; two consecutive failures → Discord | job agent down, container crashed, Postgres unavailable |
| In-app resource watch | Scheduled job reading the container's own cgroup files (`memory.current` vs `memory.max`) and `statvfs` | RAM > 85% of ceiling, disk > 80% |
| Thin-pool watch | Host cron: `lvs --noheadings -o data_percent` | **thin pool > 75%** — this one prevents data corruption |
| Pipeline liveness | `heartbeats` table; each worker updates on every loop. Dashboard shows staleness; a scheduled check alerts if any worker is stale > 3 cycles | discovery stopped, worker wedged |
| Browser health | Attempt-duration histogram + the 15-min reaper; alert if the reaper fires | stuck browser workers |
| Discord health | The outbox itself: any notification reaching `DEAD` state raises a dashboard banner, and the dead-man's switch is independent of the outbox | notifications stopped |
| Unreachable machine | The dead-man's switch runs on the host; if the *host* dies, you'll see silence in the daily summary. Optionally add a free external uptime monitor pointed at a Tailscale endpoint. | machine down |

Every one of your listed alert conditions is covered, with no new long-running services.

---

## 8. Why this architecture (and why I changed my mind)

### The recommendation

**Option C — a single unprivileged LXC with `nesting=1` and `keyctl=1`, running Docker
Compose, with the browser as a resource-capped container inside it.**

### Against each alternative

**Option B — Debian VM + Docker Compose (my previous recommendation).**
Rejected, and this is the reversal. On a 32 GB box I'd still choose the VM. On 16 GB
soldered, the deciding factor is the memory model: **a VM's RAM is committed; an LXC's is
a ceiling.** A 7 GiB VM takes 7 GiB out of 14.6 GiB permanently, even while idling at
1.5 GiB. The LXC takes what it uses. On this machine that difference is roughly 5 GiB of
usable headroom — about a third of the whole system — and there is no upgrade path to buy
it back. Add ~400 MB of guest kernel and services, slower backup and restore, and a disk
that needs partition surgery to grow instead of one `pct resize` command.

What I'm giving up, stated plainly: kernel-level isolation between Chromium and the host.
See "the one real caveat" below.

**Option A — LXC running the app natively under systemd, no Docker.**
The lightest option, and genuinely tempting. Rejected because Docker earns its ~250 MB:
per-service memory and CPU limits (which is how the browser is prevented from taking the
box down), reproducible builds, single-command rollback to a previous image, and
Playwright's official image, which saves you from resolving Chromium's ~40 shared-library
dependencies against Debian by hand and re-resolving them on every browser update.
`systemd` slices could provide the limits, but not the other three.

**Option D — multiple LXCs (separate DB, app, browser).**
Rejected. Each container adds ~120 MB of OS overhead and another `vzdump` unit to manage
and restore. Splitting Postgres onto its own container adds a network hop, a second
backup-consistency problem, and a second thing to restore in the right order — for zero
isolation benefit, since it's the same trust domain. You'd be paying complexity to
simulate a multi-node deployment you don't have.

**Option E — hybrid: LXC for the stack, small VM for the browser only.**
The strongest alternative, and the one I'd move to if the caveat below bites. It costs a
committed 3 GiB and a second guest to maintain. **The design keeps this door open at zero
cost:** the browser worker pulls work from the Postgres queue and holds no local state, so
it can be lifted into its own VM later by changing a connection string. That's deliberate.

### Scored against your criteria

| Criterion | Verdict |
|---|---|
| Resource overhead | LXC wins decisively — ~5 GiB of effective headroom vs a VM |
| Security | VM wins; LXC + unprivileged + per-container caps + domain allowlist is the mitigation |
| Docker compatibility | Fine with `nesting=1` + `keyctl=1`; standard, widely deployed |
| Playwright/browser | Works, with one caveat (below) |
| Isolation | Adequate — unprivileged user namespaces, no shared secrets volume for the browser |
| Backup | LXC wins — `vzdump` of a CT is faster and smaller than a VM image |
| Migration | LXC wins — restore to any PVE node in minutes |
| Troubleshooting | LXC wins — `pct enter 120` is instant, no SSH, no console juggling |
| Performance | LXC wins — no virtualisation layer, native I/O |
| Proxmox management | Equal |
| Long-term maintainability | Docker Compose gives reproducibility; the compose file *is* the documentation |

### The one real caveat: Chromium's sandbox inside an unprivileged LXC

Chromium's sandbox needs nested user namespaces. With `nesting=1` these are available, and
Playwright's official image plus Chrome's seccomp profile usually works — but it needs to
be *proven*, not assumed.

**This is a Phase 2 go/no-go gate**, in this order:

1. Playwright image + `security_opt: seccomp=chrome.json` + `shm_size: 1gb` → verify
   `chrome://sandbox` reports the namespace sandbox active. **Preferred.**
2. If that fails: add `cap_add: [SYS_ADMIN]` to the browser container only (still inside
   the unprivileged LXC, still capped, still no secrets volume).
3. If that fails: `--no-sandbox`, accepted **only** with the domain allowlist enforced in
   code — the browser may load Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
   Workday and PageUp domains and nothing else — and documented as a known compromise.
4. If you're not comfortable with 3: promote the browser to a 3 GiB VM (Option E). The
   architecture supports it without code changes.

We test this early precisely so the decision isn't made under pressure in Phase 15.

---

## 9. Migration from the previous guide

### KEEP — unchanged

- Entire pipeline design: discover → normalise → dedupe → hard screen → LLM enrich →
  score → route → generate → approve → submit → record
- Database schema, including the partial unique index that makes double-application a
  database constraint
- Deduplication key ladder (ATS-native key, canonical URL, trigram/simhash)
- Provenance-ID anti-fabrication mechanism and the validator
- `SUBMIT_INTENT` two-phase protocol for the browser-crash-mid-submit case
- Discord transactional outbox; webhook not bot; secret-file handling and log redaction
- Mode 2 (Approval) as default; Mode 3 allowlisted and off
- ATS-first source strategy; email-alert ingestion for SEEK/LinkedIn/APS
- Postgres queue instead of Redis + Celery — **more correct than ever on this hardware**;
  Redis would be another 100–200 MB for no benefit
- HTMX + Jinja instead of React — **more correct than ever**; a Node build toolchain and a
  second runtime on a 16 GB box would be indefensible
- Caddy, Playwright, APScheduler config, repo layout, YAML profile format
- Phase ordering (with Phase 1 rewritten)

### MODIFY

| Item | From | To |
|---|---|---|
| Phase 1 | Debian VM, cloud-init, `qm` commands | Unprivileged LXC, `pct` commands — **full rewrite** |
| Guest RAM | 12 GiB VM, balloon 4 GiB | 7 GiB LXC ceiling + 2 GiB swap |
| Guest CPU | 4 vCPU `--cpu host` | 6 LXC cores (cap), default `cpuunits` |
| Guest disk | 64 GiB VM disk | 60 GiB LXC rootfs, growable with `pct resize` |
| Postgres tuning | defaults implied | `shared_buffers=256MB`, `work_mem=8MB`, `max_connections=40`, `effective_cache_size=768MB`, `max_wal_size=1GB`, autovacuum tuned for a small dataset |
| Browser concurrency | unspecified | **1, permanently** — with the full control set in §4 |
| Firewall | `ufw` inside the guest | Proxmox CT-level firewall |
| Addressing | DHCP + router reservation | Static `192.168.1.115` in the CT config |
| TLS | Caddy internal CA | Tailscale-issued cert |
| Backups | "nightly vzdump, optional offsite" | **Off-box target is mandatory**, second M.2 recommended |
| Cost table | included a local-LLM row and lower token volume | §6 above |
| Monitoring | implied lightweight | §7.3, explicit, ~0 MB |

### REMOVE

- **Ollama and all local LLM inference** — §6
- The entire VM-based Phase 1: `qm create`, `qm importdisk`, cloud-init, `--cpu host`,
  ballooning, serial console, QEMU guest agent
- The "12 GiB RAM / 16 GiB if running Ollama" sizing table
- ZFS ARC discussion, *unless* your PVE root turns out to be ZFS (confirm in §1)
- The default recommendation of a separate browser VM — now a documented fallback

### ADD

- **BIOS pre-work** (before any Proxmox work): set VRAM/UMA allocation to minimum
  (headless box — potentially reclaims up to 1.5 GB); set power mode to **Balance 35 W**
  for 24/7 thermals; **enable Auto Power On** so the homelab returns after a power cut;
  disable Wi-Fi/Bluetooth/audio if unused
- Second M.2 NVMe as the backup target (§7.2)
- Studio-lab allocation analysis and the `memory.peak` procedure (§10)
- Crafty Controller: reduce to 3 GiB *before* it is ever started, or the ceiling budget
  breaks (13.9 + 5.59 = 19.5 GiB of ceilings against 14.6 GiB available)
- Chromium-sandbox-in-LXC go/no-go gate as a Phase 2 exit criterion (§8)
- LVM thin-pool utilisation monitoring at 75% — corruption prevention, not tidiness
- Chromium zombie reaper, `init: true`, `memswap_limit`, `pids_limit`
- Artifact retention jobs (traces 30 d / 180 d, Docker prune weekly) as first-class Phase 18 work
- Host-side dead-man's switch cron
- API spend cap in config, in addition to the provider-side cap

---

## 10. Studio-lab: analysis and recommendation

### What the numbers say

| | Allocated | Observed | Ratio |
|---|---:|---:|---:|
| CPU | 6 cores | — | — |
| RAM | 9 GiB | 676 MiB | **13.6× over-allocated at idle** |
| Swap | 2 GiB | — | |
| Disk | 78.19 GiB | check `pct df 114` | |

### CPU: change nothing

An LXC's `cores` setting is a CPU *quota* (a cap), not a reservation. Over-committing CPU
across containers is normal and harmless — the kernel scheduler arbitrates, and with 12
threads there's genuinely enough to go around for a workload that is bursty on both sides.
Reducing Studio-lab from 6 to 4 cores would gain you nothing and could throttle it during
a legitimate burst.

**Recommendation: leave Studio-lab's CPU at 6. Give job-agent 6 as well.** If contention
ever appears, the correct lever is `cpuunits` (relative weight), not core count.

### RAM: reduce the ceiling to 6 GiB, but verify peak first

The reason to act isn't to reclaim memory — as §3.2 explains, there's nothing to reclaim.
It's that **9 + 7 = 16 GiB of ceilings against 14.6 GiB available is over-committed**. If
both containers ever peaked simultaneously the host would OOM, and the OOM killer would
pick a victim on its own terms, quite possibly Postgres mid-write.

Capping Studio-lab at 6 GiB brings total ceilings to 13.9 GiB, under the 14.6 GiB
available, so **no combination of guest behaviour can OOM the host.** That's the property
worth having.

**The risks, stated properly:**

- If Studio-lab ever legitimately needs more than 6 GiB, it will be OOM-killed inside the
  container rather than allowed to grow. Whether that's acceptable depends entirely on
  what it runs.
- 676 MiB is a single point-in-time reading. It tells you nothing about peaks. A build,
  a model load, a large notebook, or a test suite could be far higher.
- If Studio-lab runs anything that loads models into memory — LM Studio, ComfyUI, Stable
  Diffusion, a Jupyter kernel doing dataframe work — **6 GiB may be genuinely too low**,
  and the right answer is to shrink job-agent instead.

**Before changing anything, measure the actual peak:**

```
pct exec 114 -- cat /sys/fs/cgroup/memory.peak      # high-water mark since boot
pct exec 114 -- cat /sys/fs/cgroup/memory.current
pct exec 114 -- ps aux --sort=-%mem | head -12
```

Then set the ceiling to **peak × 2.5, rounded up, floor of 4 GiB**.

**What makes this low-risk:** changing an LXC's memory limit is applied live to the cgroup
and takes effect in seconds, with no reboot and no restart of whatever's running inside.
If it turns out to be too low, you raise it back just as quickly. This is not a VM
resize — it's genuinely a one-command, instantly reversible experiment.

**My recommendation, conditional:**

| If Studio-lab is… | Set RAM to | Then job-agent gets |
|---|---:|---:|
| a dev sandbox, code server, notebooks, small services | **6 GiB** | 7 GiB |
| anything that loads ML models into RAM | leave at **9 GiB** | **5 GiB** — and drop the browser container to 2 GiB with `MAX_APPLICATIONS_PER_RUN: 2` |
| unclear / you'd rather not touch it | leave at **9 GiB** | 5 GiB, as above |

The 5 GiB variant still works. It's tighter on the browser and slower per batch, and I'd
want to revisit it once you've watched real memory behaviour for a fortnight.

**Do not change anything on Studio-lab until you've run the three commands above and told
me the numbers.**

### Crafty Controller

Leave it stopped. But note that at its current 5.59 GiB allocation it cannot coexist with
this design — 13.9 + 5.59 = 19.5 GiB of ceilings against 14.6 GiB available. Before you
ever start it, reduce it to **3 GiB** (ample for a small Minecraft server with a 2 GB JVM
heap) and treat it as mutually exclusive with browser batches. Its 32 GiB disk allocation
is thin-provisioned and costs you almost nothing while stopped.

---

## 11. Summary resource budget

| Component | CPU | RAM ceiling | RAM actual | Storage |
|---|---:|---:|---:|---:|
| Proxmox VE host | shared | 0.90 GiB | 0.85 GiB | ~12 GiB used of ~100 GiB `local` |
| ZFS ARC (only if ZFS root) | — | 1.00 GiB | varies | — |
| CT 114 studio-lab | 6 (cap) | 6.00 GiB† | 0.66 GiB | 78.19 GiB (thin) |
| CT 120 job-agent | 6 (cap) | 7.00 GiB† | 1.5–5.8 GiB | 60 GiB (thin) |
|  └ postgres | 1.5 | 1.00 GiB | 0.45 GiB | 6 GiB |
|  └ browser (Chromium) | 2.0 | 2.50 GiB | 0.9–1.6 GiB | 3 GiB tmp |
|  └ worker | 2.0 | 0.75 GiB | 0.25 GiB | — |
|  └ api | 1.0 | 0.50 GiB | 0.18 GiB | — |
|  └ scheduler + notifier + caddy | 1.5 | 0.56 GiB | 0.16 GiB | — |
|  └ LXC OS + dockerd | — | 0.50 GiB | 0.38 GiB | 14.5 GiB (images + cache) |
| CT ??? crafty (stopped) | 2 | 0 | 0 | 32 GiB (thin, ~3 GiB written) |
| On-box backups | — | — | — | 40 GiB on `local` |
| **Reserved headroom** | 12 threads shared | **0.70 GiB worst case / ~12.1 GiB typical** | | **~180 GiB unallocated thin pool + ~48 GiB `local`** |

† conditional on the Studio-lab measurement in §10.

---

## 12. What I need from you before Phase 1

1. Output of the seven commands in §1 — especially whether root is ext4/LVM or ZFS, and
   what `free -h` reports (this tells us the iGPU UMA setting).
2. Output of the three `pct exec 114` commands in §10, and **what Studio-lab actually
   runs**. This is the one genuine unknown in the whole budget.
3. Studio-lab's real VMID and Crafty's VMID.
4. A decision on the second M.2 NVMe — it changes the backup section from "adequate" to
   "actually safe", and it's the only upgrade this machine will ever accept.
5. Your approval of the allocation in §11, or the numbers you want changed.

Once those land, I'll write the rewritten **Phase 1 — Proxmox LXC Foundation** against
your real environment: BIOS pre-work, CT creation with nesting/keyctl, static networking,
PVE firewall rules, Tailscale, Docker, backup storage, and a verification checklist that
includes the Chromium sandbox gate.
