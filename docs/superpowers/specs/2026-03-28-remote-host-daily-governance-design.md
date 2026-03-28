# Remote Host Daily Governance — Design Spec

**Date**: 2026-03-28
**Scope**: Single remote host (`charl@100.91.176.126:~/subhft`) daily maintenance automation
**Approach**: Pure shell scripts, zero new dependencies, integrated into existing cron + Telegram notification system

---

## Problem Statement

The remote host relies on manual SSH intervention for OS patching, secret lifecycle tracking, and pre-deployment validation. There is no automated health baseline check before deployments, no secret age tracking, and no consolidated daily health report beyond component-specific soak reports.

## Design Decisions

1. **Pure shell** — maintenance scripts must run even if Python environment is broken.
2. **Telegram notifications** via `curl` — reuses existing `HFT_TELEGRAM_BOT_TOKEN` / `HFT_TELEGRAM_CHAT_ID` from `.env`.
3. **Shared notify helper** — `scripts/_notify.sh` sourced by all governance scripts.
4. **No auto-reboot** — security updates apply automatically but reboot requires manual action.
5. **Secret age via `.env` comments** — `# ROTATED: YYYY-MM-DD` annotations, not external state files.

---

## Components

### 1. `scripts/_notify.sh` — Shared Telegram Helper

Sourced by all governance scripts. Provides:

```bash
notify_telegram() {
    local msg="$1"
    [ -z "${HFT_TELEGRAM_BOT_TOKEN:-}" ] && return 0
    curl -sf -X POST \
        "https://api.telegram.org/bot${HFT_TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${HFT_TELEGRAM_CHAT_ID}" \
        -d parse_mode=Markdown \
        -d text="${msg}" > /dev/null 2>&1 || true
}
```

Also loads `.env` if present (for BOT_TOKEN / CHAT_ID).

### 2. `scripts/host_preflight.sh` — Pre-Deploy Baseline Verification

**Trigger**: Manual or auto-called by `deploy.sh` before deployment.
**Exit**: 0 = all pass, 1 = any fail (blocks deploy).

| Check | Method | FAIL Condition |
|-------|--------|----------------|
| Docker version | `docker --version` | < 24.0 |
| Docker Compose | `docker compose version` | Not found |
| Disk available | `df --output=pcent /` | Available < 20% |
| Memory available | `free -m` | Available < 512 MB |
| Required ports | `ss -tlnp` on 8123, 9090, 6379 | Port occupied by unexpected process |
| `.env` validity | Calls `validate_env.sh` | exit != 0 |
| Docker services | `docker compose ps` | Any service not running/healthy |
| Core sysctl | Check `net.core.rmem_max` etc. | Doesn't match `ops.sh tune` values |

### 3. `scripts/host_security_update.sh` — Weekly Security Patching

**Trigger**: Cron, weekly Sunday 03:30.

Flow:
1. `apt update`
2. Count security-upgradable packages (`unattended-upgrades --dry-run`)
3. If updates available: apply via `unattended-upgrades` (uses `/etc/apt/apt.conf.d/50unattended-upgrades` security-only origin pattern)
4. Log update list to `/var/log/hft_security_updates.log`
5. Check `/var/run/reboot-required` — if exists, Telegram warns (no auto-reboot)
6. Telegram summary: `N packages updated / 0 pending / reboot needed: yes|no`

Does NOT touch Docker version — Docker upgrades are higher-risk, reported only by preflight.

### 4. `scripts/secret_age_check.sh` — Daily Secret Rotation Reminder

**Trigger**: Cron, daily 07:00 Mon-Fri (before market open).

Tracking mechanism — `.env` annotations:
```bash
CLICKHOUSE_PASSWORD=xxx   # ROTATED: 2026-03-01
REDIS_PASSWORD=xxx        # ROTATED: 2026-03-01
SHIOAJI_API_KEY=xxx       # ROTATED: 2026-02-15
SHIOAJI_SECRET_KEY=xxx    # ROTATED: 2026-02-15
HFT_TELEGRAM_BOT_TOKEN=xxx  # ROTATED: 2026-01-20
```

Logic:
1. Parse all `# ROTATED: YYYY-MM-DD` annotations in `.env`
2. Calculate days since rotation for each secret
3. Threshold: 90 days (override via `SECRET_MAX_AGE_DAYS` env var)
4. Over threshold → Telegram: `Secret CLICKHOUSE_PASSWORD aged 102 days (max: 90)`
5. Missing `# ROTATED:` annotation → treated as "never rotated", always warns

Tracked secrets (hardcoded list):
- `CLICKHOUSE_PASSWORD`
- `REDIS_PASSWORD`
- `SHIOAJI_API_KEY`
- `SHIOAJI_SECRET_KEY`
- `HFT_TELEGRAM_BOT_TOKEN`

### 5. `scripts/host_health_report.sh` — Daily Health Summary

**Trigger**: Cron, daily 16:45 Mon-Fri (after market close, after soak report at 16:10).

| Metric | Source | Alert Condition |
|--------|--------|-----------------|
| Disk usage | `df` | > 80% |
| Memory usage | `free` | > 90% |
| CPU load | `uptime` (15m avg) | > number of CPUs |
| Docker services | `docker compose ps` | Any not running/healthy |
| ClickHouse lag | `curl localhost:8123` query latest data timestamp | > 30 min during trading hours |
| System uptime | `uptime` | < 1 day (unexpected restart) |
| Pending reboot | `/var/run/reboot-required` | File exists |

Telegram output (normal):
```
HFT Host Daily Health (2026-03-28)
Disk: 45% | RAM: 62% | Load: 0.8
Docker: 6/6 healthy
CH lag: 2m | Uptime: 26d
Secrets: all OK
```

Anomalies marked with warning prefix. All-clear = single compact message.

---

## Cron Schedule

Added to `docs/operations/cron-setup-remote.md`:

```cron
# --- Host Security Update (weekly Sunday 03:30) ---
30 3 * * 0 cd /home/charl/subhft && bash scripts/host_security_update.sh >> /tmp/hft_security_update.log 2>&1

# --- Secret Age Check (daily 07:00, before market open) ---
0 7 * * 1-5 cd /home/charl/subhft && bash scripts/secret_age_check.sh >> /tmp/hft_secret_age.log 2>&1

# --- Host Health Report (daily 16:45, after market close) ---
45 16 * * 1-5 cd /home/charl/subhft && bash scripts/host_health_report.sh >> /tmp/hft_host_health.log 2>&1
```

Schedule rationale:
- Security update: Sunday 03:30 — avoids trading days, offsets from Docker prune (1st of month 03:00)
- Secret check: 07:00 — before TWSE open (08:45), gives time to act
- Health report: 16:45 — after close, after existing soak report (16:10)
- Preflight: no cron — manual or deploy.sh triggered

---

## Integration Points

### deploy.sh Gate

Add preflight as first step in `scripts/deploy.sh`:
```bash
echo ">> Running host preflight..."
bash "$(dirname "$0")/host_preflight.sh" || { echo "FATAL: Preflight failed. Aborting deploy."; exit 1; }
```

### .env Annotation

Add `# ROTATED: YYYY-MM-DD` comments to existing secrets in `.env` on the remote host. No schema change; purely comment-based.

---

## File Manifest

| Action | File |
|--------|------|
| Create | `scripts/_notify.sh` |
| Create | `scripts/host_preflight.sh` |
| Create | `scripts/host_security_update.sh` |
| Create | `scripts/secret_age_check.sh` |
| Create | `scripts/host_health_report.sh` |
| Modify | `scripts/deploy.sh` (add preflight gate) |
| Modify | `docs/operations/cron-setup-remote.md` (add 3 cron entries) |

No new Python dependencies. No changes to platform core code.

---

## Out of Scope

- Infrastructure as Code (Ansible/Terraform) — single host, shell sufficient
- Automatic secret rotation — reminder-only per user preference
- Docker version auto-upgrade — reported by preflight, manual action
- Multi-host / HA failover — single host deployment
- Automatic reboot after kernel updates — notify only
- Offsite backup / DR plan — separate initiative
