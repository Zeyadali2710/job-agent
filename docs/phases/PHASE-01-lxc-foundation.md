# PHASE 1 — Proxmox LXC Foundation (rewritten for your environment)

**Supersedes the VM-based `PHASE-01-proxmox-foundation.md` entirely.**

Confirmed environment: PVE 9.2.11, kernel 7.0.14-15-pve, ext4/LVM root (no ZFS),
`local` 93.9 GiB (10.4 used), `local-lvm` thin pool 348.8 GiB (46.9 written, 136 allocated),
guests 100/101/104 running, 102/103/200 stopped.

Target: **CT 120 `job-agent`** — unprivileged, nesting enabled, Docker installed,
static `192.168.1.115`, firewalled, backed up, monitored by your existing Uptime Kuma.

**Do not proceed to Phase 2 until every box in §11 is ticked.**

---

## 1. Objective

A container that runs the job-agent stack 24/7 on a budget that cannot starve the rest of
your homelab, with correct Sydney time, automatic restart after power loss, and a tested
backup.

No application code in this phase.

## 2. Final resource allocation

| Setting | Value | Reason |
|---|---|---|
| VMID | 120 | clear of your 100–104 block and the 200 VM |
| Type | unprivileged LXC | memory is a ceiling, not a reservation — §8 of the architecture doc |
| Features | `nesting=1,keyctl=1` | both required for Docker inside an unprivileged container |
| Cores | 6 | a cap, not a reservation; 12 threads available, overcommit is harmless |
| Memory | 8192 MiB | leaves ~1.0 GiB free with every guest at peak |
| Swap | 2048 MiB | absorbs brief overshoot instead of triggering an OOM kill |
| Rootfs | `local-lvm:64` | takes thin-pool allocation to 200 of 348.8 GiB (57%) |
| Net | `vmbr0`, static `192.168.1.115/24`, gw `192.168.1.1`, `firewall=1` | |
| onboot | 1, `startup: order=2` | returns after a power cut |
| Template | Debian 13 standard | |

## 3. Prerequisites

- [ ] Physical access to the M8 for a BIOS reboot (§4)
- [ ] `192.168.1.115` is outside your router's DHCP pool, or excluded from it
- [ ] Your SSH public key to hand
- [ ] A Tailscale account
- [ ] Uptime Kuma (CT 100) reachable, with a Discord webhook you can paste into it
- [ ] ~20 minutes of homelab downtime for the BIOS reboot

---

## 4. BIOS pre-work — do this first

`free -h` reported 12 GiB on a 16 GiB machine. Roughly 3.5 GiB is reserved for the Radeon
660M on a server with nothing plugged into its display outputs.

Shut down all guests, then the host:

```bash
for i in 100 101 104; do pct shutdown $i; done
shutdown -h now
```

Power on, tap `Del` (or `F2`) to enter BIOS, and set:

| Setting | Value | Why |
|---|---|---|
| **UMA / VRAM allocation** | **minimum (512M)** — often under Advanced → AMD CBS → NBIO → GFX, or a top-level "VRAM Allocation" item | recovers ~3.3 GiB |
| **Auto Power On / Restore on AC Power** | **Enabled / Last State** | homelab returns after a power cut without you noticing |
| Performance mode | **Balance (35 W)** | cooler and quieter for 24/7; this workload is bursty, not sustained |
| Wi-Fi / Bluetooth / Audio | Disabled if unused | fewer drivers, fewer surprises |

Save, reboot, then verify:

```bash
free -h
```

**Expect `total` around 15Gi.** If it still reads 12Gi, the setting is named differently
or is locked — tell me before continuing and we'll fall back to Scenario B (job-agent at
6144 MiB, browser at 2560 MiB). Everything else in this document is unchanged.

## 5. Studio-lab ceiling

Live change, no restart, reversible in seconds:

```bash
pct set 104 --memory 4096
pct config 104 | grep memory
pct exec 104 -- cat /sys/fs/cgroup/memory.current
```

The reported current usage should be well under 4 GiB (you measured 676 MiB). If Studio-lab
ever misbehaves after this, `pct set 104 --memory 9216` puts it straight back.

## 6. Create the container

### 6.1 Template

```bash
pveam update
pveam available --section system | grep debian-13
```

Download the standard image (substitute the exact filename from the output):

```bash
pveam download local debian-13-standard_13.1-1_amd64.tar.zst
pveam list local
```

### 6.2 Create

```bash
pct create 120 local:vztmpl/debian-13-standard_13.1-1_amd64.tar.zst \
  --hostname job-agent \
  --unprivileged 1 \
  --features nesting=1,keyctl=1 \
  --cores 6 \
  --memory 8192 \
  --swap 2048 \
  --rootfs local-lvm:64 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.1.115/24,gw=192.168.1.1,firewall=1 \
  --nameserver "1.1.1.1 9.9.9.9" \
  --onboot 1 \
  --startup order=2 \
  --ostype debian \
  --description "Job Agent — see docs/01-ARCHITECTURE-REVISED-gmktec-m8.md"
```

`keyctl=1` matters specifically: without it Docker's containerd fails to start in an
unprivileged container.

### 6.3 TUN device for Tailscale

Start it and check whether PVE already provides `/dev/net/tun`:

```bash
pct start 120
pct exec 120 -- ls -l /dev/net/tun
```

If that errors, add the passthrough:

```bash
pct stop 120
cat >> /etc/pve/lxc/120.conf <<'EOF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
EOF
pct start 120
pct exec 120 -- ls -l /dev/net/tun
```

### 6.4 Root password and shell

```bash
pct exec 120 -- passwd root
pct enter 120
```

`pct enter` is your always-available console — no SSH dependency, no network dependency.
This is one of the main reasons the LXC is easier to troubleshoot than a VM.

---

## 7. Configure inside the container

Everything in this section runs inside `pct enter 120`.

### 7.1 Base, timezone, user

```bash
apt update && apt upgrade -y
apt install -y curl ca-certificates gnupg jq git htop ncdu \
               unattended-upgrades apt-listchanges openssh-server

timedatectl set-timezone Australia/Sydney
date
```

The container shares the host's clock, so NTP is the host's job — verify on the **host**
with `timedatectl` that `System clock synchronized: yes`.

Create your working user:

```bash
adduser --gecos "" yourname
usermod -aG sudo yourname
mkdir -p /home/yourname/.ssh
# paste your public key:
nano /home/yourname/.ssh/authorized_keys
chmod 700 /home/yourname/.ssh
chmod 600 /home/yourname/.ssh/authorized_keys
chown -R yourname:yourname /home/yourname/.ssh
```

### 7.2 SSH hardening

```bash
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
X11Forwarding no
EOF

sshd -t && systemctl restart ssh
```

Test from your workstation **before** closing the console: `ssh yourname@192.168.1.115`.
If it fails, you still have `pct enter 120` — you cannot lock yourself out of an LXC.

### 7.3 Docker

```bash
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | \
  gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list

apt update
apt install -y docker-ce docker-ce-cli containerd.io \
               docker-buildx-plugin docker-compose-plugin

usermod -aG docker yourname
```

Log rotation, so Docker can't slowly consume the 64 GiB:

```bash
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "live-restore": true,
  "storage-driver": "overlay2"
}
EOF

systemctl restart docker
docker run --rm hello-world
docker compose version
docker info | grep -E 'Storage Driver|Live Restore|Cgroup Version'
```

Expect `overlay2`, `Live Restore Enabled: true`, `Cgroup Version: 2`. If Docker fails to
start here, `keyctl=1` is almost certainly missing — see §10.

### 7.4 Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --ssh --hostname=job-agent
```

Authenticate at the printed URL. Then in the Tailscale admin console, find `job-agent`
and **disable key expiry** — otherwise the node silently deauthenticates in six months.

```bash
tailscale status
tailscale ip -4
```

### 7.5 Automatic security updates

```bash
dpkg-reconfigure -plow unattended-upgrades   # Yes

cat > /etc/apt/apt.conf.d/51-job-agent <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF

unattended-upgrades --dry-run --debug 2>&1 | tail -15
```

Automatic reboot stays off deliberately — you don't want a restart mid-submission.

### 7.6 Directory layout

```bash
mkdir -p /opt/job-agent/{data,logs,secrets,backups,artifacts}
chown -R yourname:yourname /opt/job-agent
chmod 700 /opt/job-agent/secrets
```

Phase 2 places Docker secret files in `secrets/` at mode 600.

Exit the container: `exit`.

---

## 8. Firewall

**Read this whole section before running anything.** Enabling the datacenter firewall with
a default DROP input policy and no host rules will lock you out of the web UI.

### 8.1 Allow host management first

```bash
mkdir -p /etc/pve/firewall
cat > /etc/pve/firewall/cluster.fw <<'EOF'
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -source 192.168.1.0/24 -p tcp -dport 8006 -log nolog # PVE web UI
IN ACCEPT -source 192.168.1.0/24 -p tcp -dport 22 -log nolog   # host SSH
IN ACCEPT -p icmp -log nolog
EOF
```

Immediately confirm the web UI still loads and your SSH session survives. If you lose
access, get to the machine's physical console and run:

```bash
pvesh set /cluster/firewall/options --enable 0
```

### 8.2 Container rules

```bash
cat > /etc/pve/firewall/120.fw <<'EOF'
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -source 192.168.1.0/24 -p tcp -dport 22 -log nolog  # SSH from LAN
IN ACCEPT -source 192.168.1.0/24 -p tcp -dport 443 -log nolog # dashboard (Phase 2)
IN ACCEPT -p icmp -log nolog
EOF
```

Note that Tailscale traffic arrives on `tailscale0` **inside** the container and bypasses
these bridge rules entirely. Tailnet ingress is governed by your Tailscale ACLs, not by
this file — that's expected, and it's why the dashboard will still need its own
authentication in Phase 2.

Verify from a LAN machine that nothing extra is exposed:

```bash
nmap -Pn -p- --min-rate 2000 192.168.1.115 | grep -v closed
```

Expect `22/tcp open` and nothing else.

---

## 9. Backup and monitoring

### 9.1 Backup

`local` has 78.7 GiB free, but Kali (VM 200, 40 GB) and Jellyfin (CT 103) also live there
and are currently sparse. Keep the on-box footprint modest until the second NVMe arrives.

Datacenter → Backup → Add:

- Schedule `03:15`
- Selection: CT **120** and CT **104**
- Storage: `local`
- Mode: Snapshot, Compression: ZSTD
- Retention: **Keep Daily 2, Keep Weekly 1**

That's roughly 14–19 GiB. Run one now and verify a restore:

```bash
vzdump 120 --storage local --mode snapshot --compress zstd
ls -lh /var/lib/vz/dump/

# restore to a throwaway VMID
pct restore 920 /var/lib/vz/dump/vzdump-lxc-120-*.tar.zst --storage local-lvm
pct start 920 && pct exec 920 -- docker --version
pct stop 920 && pct destroy 920
```

A backup you haven't restored is a hypothesis. Do this once, now.

**Still outstanding:** an off-box target. Until a second NVMe is in the free M.2 slot,
these backups do not survive the SSD failing. Everything irreplaceable — your profile,
criteria, application history — will be a few hundred MB, so a private git repo covers the
worst case from Phase 4 onward.

### 9.2 Snapshot

```bash
pct snapshot 120 phase1-clean --description "Debian 13 + docker + tailscale + hardened"
pct listsnapshot 120
```

Take one before each phase. Rollback is `pct rollback 120 phase1-clean`.

### 9.3 Uptime Kuma

In CT 100:

1. Settings → Notifications → Setup Notification → **Discord**, paste your webhook, and
   send a test. Name it `discord-homelab`.
2. Add Monitor → **TCP Port** → `192.168.1.115:22`, 60 s interval, retries 2, attach the
   Discord notification. This proves the container is alive from Phase 1 onward.
3. Phase 2 replaces this with an HTTP monitor on `/health` and adds a push monitor the
   scheduler pings each cycle.

This replaces the custom watchdog cron I originally specified — you already had the better
tool installed.

**One gap to close later:** Uptime Kuma dies with the host, so it can't report the host
being down. Phase 18 adds a free healthchecks.io ping from the job agent, outbound only.

---

## 10. Common errors

**`docker: failed to start containerd` / `failed to create shim`**
`keyctl=1` missing. `pct set 120 --features nesting=1,keyctl=1` then `pct stop 120 && pct start 120`.
A *reboot* isn't enough — features are applied at container start.

**`overlay2` unavailable, Docker falls back to `vfs`**
`vfs` will consume the 64 GiB rapidly. On PVE 9 with cgroup v2 and `nesting=1` this
shouldn't happen; if it does, confirm the container is unprivileged and nesting is set,
and check `dmesg` on the host for AppArmor denials.

**Tailscale: `failed to connect to local tailscaled` or TUN errors**
`/dev/net/tun` isn't present — §6.3.

**No network after creation**
`pct config 120 | grep net0` — confirm the gateway is right for your LAN, and that
`192.168.1.115` isn't already leased to something by DHCP. `pct enter 120` always works
regardless of networking.

**Locked out of the PVE web UI after §8**
Physical console, `pvesh set /cluster/firewall/options --enable 0`.

**`pveam download` fails or the template name doesn't match**
Re-run `pveam available --section system | grep debian-13` and use the exact filename.

**Container won't start after a host reboot**
`pct config 120 | grep -E 'onboot|startup'` should show `onboot: 1` and `startup: order=2`.

**Thin pool warnings**
`lvs -o lv_name,data_percent pve/data`. You're at 13.5% written; alert threshold is 75%.
A full thin pool corrupts the volumes on it — this is the one storage number to watch.

### Troubleshooting order

1. `pct enter 120` then `journalctl -xe -n 100`
2. `systemctl status docker tailscaled ssh`
3. On the host: `pct config 120` — is it configured how you think?
4. Host: `dmesg -T | tail -50` for AppArmor or cgroup denials
5. `pct rollback 120 phase1-clean` and redo the step

---

## 11. Completion checklist

- [ ] `free -h` on the host shows **~15Gi total** (BIOS VRAM fixed)
- [ ] BIOS Auto Power On enabled; power mode Balance 35 W
- [ ] `pct config 104 | grep memory` shows `4096`
- [ ] `pct config 120` shows `unprivileged: 1`, `features: keyctl=1,nesting=1`, `cores: 6`, `memory: 8192`, `swap: 2048`, `onboot: 1`, `startup: order=2`
- [ ] `pct exec 120 -- ls -l /dev/net/tun` succeeds
- [ ] `date` inside the container shows correct Sydney time
- [ ] `docker run --rm hello-world` succeeds **as your non-root user**
- [ ] `docker info` shows `overlay2`, `Live Restore Enabled: true`, `Cgroup Version: 2`
- [ ] `ssh yourname@192.168.1.115` works; password auth and root login both refused
- [ ] `ssh yourname@job-agent` works over Tailscale from your phone
- [ ] Tailscale key expiry disabled for this node
- [ ] `nmap` from the LAN shows only port 22 open
- [ ] PVE web UI still reachable after enabling the firewall
- [ ] `/opt/job-agent/secrets` exists at mode 700
- [ ] `pct listsnapshot 120` shows `phase1-clean`
- [ ] A vzdump archive exists **and has been restored to CT 920 successfully**
- [ ] Nightly backup job scheduled for 120 and 104
- [ ] Uptime Kuma TCP monitor on `192.168.1.115:22` is green, with a **tested** Discord notification
- [ ] Container survives `pct reboot 120` with Docker and Tailscale back automatically
- [ ] Container starts automatically after a **host** reboot (`reboot`, then `pct list`)
- [ ] `lvs -o lv_name,data_percent pve/data` under 60%

## 12. Record before Phase 2

```
CT ID:              120
LAN IP:             192.168.1.115
Tailscale IP:       ___________________
MagicDNS name:      job-agent.__________.ts.net
Username:           ___________________
Host RAM after BIOS fix: ___________________
Thin pool data%:    ___________________
Snapshot:           phase1-clean
```

---

**Next:** Phase 2 — repository skeleton, Docker Compose with the per-container memory
limits from §11 of the architecture doc, Caddy with Tailscale certificates, the secrets
pattern, and the **Chromium sandbox go/no-go test** that decides whether the browser stays
in this container or gets promoted to its own VM.

Tell me when the checklist is complete, and paste anything that failed.
