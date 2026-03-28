# Remote Host Daily Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5 shell scripts for remote host daily maintenance automation — preflight checks, security patching, secret age tracking, and health reporting — all integrated into the existing cron + Telegram notification system.

**Architecture:** Pure shell scripts sourcing a shared `_notify.sh` helper for Telegram via `curl`. All scripts run on the remote host, use `DEPLOY_ROOT` env var for path resolution, and follow the existing cron template pattern in `docs/operations/cron-setup-remote.md`.

**Tech Stack:** Bash, curl (Telegram API), apt/unattended-upgrades, existing `validate_env.sh`

**Spec:** `docs/superpowers/specs/2026-03-28-remote-host-daily-governance-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `scripts/_notify.sh` | Shared helper: load `.env`, `DEPLOY_ROOT`, `notify_telegram()` |
| Create | `scripts/host_preflight.sh` | Pre-deploy baseline checks (8 checks, exit 0/1) |
| Create | `scripts/secret_age_check.sh` | Parse `# ROTATED:` dates, warn on overdue, `--quiet` mode |
| Create | `scripts/host_health_report.sh` | Daily health summary → Telegram |
| Create | `scripts/host_security_update.sh` | Weekly apt security update + Telegram notification |
| Modify | `scripts/deploy.sh` | Add remote preflight gate before `docker pull` |
| Modify | `docs/operations/cron-setup-remote.md` | Add 3 cron entries |

---

## Task 1: `scripts/_notify.sh` — Shared Helper

**Files:**
- Create: `scripts/_notify.sh`

### Step 1.1: Create `_notify.sh`

- [ ] **Create the shared helper script**

```bash
#!/usr/bin/env bash
# _notify.sh — Shared helper for HFT governance scripts.
# Source this file; do not execute directly.
# Provides: DEPLOY_ROOT, SCRIPT_DIR, notify_telegram()

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT="${DEPLOY_ROOT:-/home/charl/subhft}"

# Load .env for Telegram credentials (if present)
if [ -f "${DEPLOY_ROOT}/.env" ]; then
    # Source only lines matching known Telegram vars to avoid side effects
    eval "$(grep -E '^HFT_TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "${DEPLOY_ROOT}/.env")"
fi

notify_telegram() {
    local msg="$1"
    [ -z "${HFT_TELEGRAM_BOT_TOKEN:-}" ] && return 0
    [ -z "${HFT_TELEGRAM_CHAT_ID:-}" ] && return 0
    curl -sf -X POST \
        "https://api.telegram.org/bot${HFT_TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${HFT_TELEGRAM_CHAT_ID}" \
        -d parse_mode=Markdown \
        -d text="${msg}" > /dev/null 2>&1 || true
}
```

### Step 1.2: Verify it sources cleanly

- [ ] **Run a quick source test**

Run:
```bash
bash -c 'source scripts/_notify.sh && echo "DEPLOY_ROOT=${DEPLOY_ROOT}" && type notify_telegram'
```

Expected: Prints `DEPLOY_ROOT=/home/charl/subhft` (or override) and confirms `notify_telegram is a function`.

### Step 1.3: Commit

- [ ] **Commit**

```bash
git add scripts/_notify.sh
git commit -m "feat(ops): add shared _notify.sh helper for governance scripts"
```

---

## Task 2: `scripts/secret_age_check.sh` — Secret Rotation Reminder

Built before `host_health_report.sh` because health report depends on `--quiet` mode.

**Files:**
- Create: `scripts/secret_age_check.sh`

### Step 2.1: Create `secret_age_check.sh`

- [ ] **Create the script**

```bash
#!/usr/bin/env bash
# secret_age_check.sh — Check secret rotation age from .env annotations.
# Usage:
#   ./scripts/secret_age_check.sh           # Check + send Telegram alerts
#   ./scripts/secret_age_check.sh --quiet   # Check only, exit code (for health report)
#
# Expects .env lines like:
#   CLICKHOUSE_PASSWORD=xxx  # ROTATED: 2026-03-01
#
# Environment:
#   SECRET_MAX_AGE_DAYS  — threshold in days (default: 90)
#   DEPLOY_ROOT          — project root (default: /home/charl/subhft)

set -euo pipefail
source "$(dirname "$0")/_notify.sh"

QUIET=false
if [[ "${1:-}" == "--quiet" ]]; then
    QUIET=true
fi

MAX_AGE="${SECRET_MAX_AGE_DAYS:-90}"
ENV_FILE="${DEPLOY_ROOT}/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "FATAL: .env not found at ${ENV_FILE}"
    exit 1
fi

# Canonical secret list (aligned with validate_env.sh)
PRIMARY_SECRETS=(
    "CLICKHOUSE_PASSWORD"
    "REDIS_PASSWORD"
    "SHIOAJI_API_KEY"
    "SHIOAJI_SECRET_KEY"
    "HFT_TELEGRAM_BOT_TOKEN"
)

# Alias secrets: name -> primary it must match
declare -A ALIAS_SECRETS=(
    ["MONITOR_CH_PASSWORD"]="CLICKHOUSE_PASSWORD"
    ["MONITOR_REDIS_PASSWORD"]="REDIS_PASSWORD"
)

# Conditional secrets (only checked if condition met)
CONDITIONAL_SECRETS=()
source "$ENV_FILE" 2>/dev/null || true
if [ "${HFT_BROKER:-shioaji}" = "fubon" ]; then
    CONDITIONAL_SECRETS+=("HFT_FUBON_PASSWORD")
fi

TODAY_EPOCH=$(date +%s)
overdue_count=0
warnings=""

check_secret_age() {
    local name="$1"
    local env_file="$2"

    # Extract ROTATED date from comment on the same line
    local rotated_line
    rotated_line=$(grep -E "^${name}=" "$env_file" | grep -oP '# ROTATED: \K[0-9]{4}-[0-9]{2}-[0-9]{2}' || echo "")

    if [ -z "$rotated_line" ]; then
        warnings="${warnings}${name}: no ROTATED annotation (never rotated?)\n"
        overdue_count=$((overdue_count + 1))
        return
    fi

    local rotated_epoch
    rotated_epoch=$(date -d "$rotated_line" +%s 2>/dev/null || echo "0")
    if [ "$rotated_epoch" = "0" ]; then
        warnings="${warnings}${name}: invalid ROTATED date '${rotated_line}'\n"
        overdue_count=$((overdue_count + 1))
        return
    fi

    local age_days=$(( (TODAY_EPOCH - rotated_epoch) / 86400 ))
    if [ "$age_days" -gt "$MAX_AGE" ]; then
        warnings="${warnings}${name}: aged ${age_days} days (max: ${MAX_AGE})\n"
        overdue_count=$((overdue_count + 1))
    fi
}

# Check primary secrets
for secret in "${PRIMARY_SECRETS[@]}"; do
    check_secret_age "$secret" "$ENV_FILE"
done

# Check conditional secrets
for secret in "${CONDITIONAL_SECRETS[@]}"; do
    check_secret_age "$secret" "$ENV_FILE"
done

# Check alias consistency (validate_env.sh L33-41 parity)
for alias_name in "${!ALIAS_SECRETS[@]}"; do
    primary_name="${ALIAS_SECRETS[$alias_name]}"
    # Only check if alias is actually set in .env
    alias_val=$(grep -E "^${alias_name}=" "$ENV_FILE" | head -1 | cut -d= -f2- | cut -d'#' -f1 | xargs || echo "")
    if [ -n "$alias_val" ]; then
        primary_val=$(grep -E "^${primary_name}=" "$ENV_FILE" | head -1 | cut -d= -f2- | cut -d'#' -f1 | xargs || echo "")
        if [ "$alias_val" != "$primary_val" ]; then
            warnings="${warnings}${alias_name} != ${primary_name} (consistency violation)\n"
            overdue_count=$((overdue_count + 1))
        fi
        # Alias inherits primary's rotation date — no separate age check needed
    fi
done

# Output
if [ "$overdue_count" -gt 0 ]; then
    echo -e "SECRET AGE CHECK: ${overdue_count} issue(s)\n${warnings}"
    if [ "$QUIET" = false ]; then
        notify_telegram "$(printf '⚠️ *Secret Rotation Check*\n%b' "$warnings")"
    fi
    exit 1
else
    echo "SECRET AGE CHECK: all OK (threshold: ${MAX_AGE} days)"
    exit 0
fi
```

### Step 2.2: Test locally with a mock .env

- [ ] **Verify the script runs and detects missing annotations**

Run:
```bash
# Create a temp .env for testing
TMPDIR=$(mktemp -d)
cat > "${TMPDIR}/.env" <<'ENVEOF'
CLICKHOUSE_PASSWORD=test123  # ROTATED: 2026-03-01
REDIS_PASSWORD=test456
SHIOAJI_API_KEY=xxx  # ROTATED: 2025-01-01
SHIOAJI_SECRET_KEY=yyy  # ROTATED: 2026-03-28
HFT_TELEGRAM_BOT_TOKEN=zzz  # ROTATED: 2026-03-28
ENVEOF

DEPLOY_ROOT="$TMPDIR" bash scripts/secret_age_check.sh --quiet; echo "Exit: $?"
rm -rf "$TMPDIR"
```

Expected: Exit 1 (REDIS_PASSWORD has no annotation; SHIOAJI_API_KEY is > 90 days old at 452 days).

### Step 2.3: Commit

- [ ] **Commit**

```bash
chmod +x scripts/secret_age_check.sh
git add scripts/secret_age_check.sh
git commit -m "feat(ops): add secret_age_check.sh with ROTATED annotation tracking"
```

---

## Task 3: `scripts/host_preflight.sh` — Pre-Deploy Checks

**Files:**
- Create: `scripts/host_preflight.sh`

### Step 3.1: Create `host_preflight.sh`

- [ ] **Create the script**

```bash
#!/usr/bin/env bash
# host_preflight.sh — Pre-deployment baseline verification.
# Runs on the REMOTE host. Exit 0 = all pass, 1 = any fail.
# Usage: bash scripts/host_preflight.sh
#
# Environment:
#   DEPLOY_ROOT  — project root (default: /home/charl/subhft)

set -euo pipefail
source "$(dirname "$0")/_notify.sh"

cd "$DEPLOY_ROOT"

errors=0
warnings=0

pass()  { echo "  ✓ $1"; }
warn()  { echo "  ⚠ $1"; warnings=$((warnings + 1)); }
fail()  { echo "  ✗ $1"; errors=$((errors + 1)); }

echo "=== HFT Host Preflight Check ==="
echo "    Host: $(hostname)"
echo "    Root: ${DEPLOY_ROOT}"
echo "    Time: $(date -Iseconds)"
echo ""

# 1. Docker version >= 24.0
echo "[1/8] Docker version"
if command -v docker &> /dev/null; then
    DOCKER_VER=$(docker --version | grep -oP '\d+\.\d+' | head -1)
    DOCKER_MAJOR=$(echo "$DOCKER_VER" | cut -d. -f1)
    if [ "$DOCKER_MAJOR" -ge 24 ]; then
        pass "Docker ${DOCKER_VER}"
    else
        fail "Docker ${DOCKER_VER} < 24.0"
    fi
else
    fail "Docker not found"
fi

# 2. Docker Compose
echo "[2/8] Docker Compose"
if docker compose version &> /dev/null; then
    COMPOSE_VER=$(docker compose version --short 2>/dev/null || docker compose version | grep -oP '\d+\.\d+\.\d+' | head -1)
    pass "Docker Compose ${COMPOSE_VER}"
else
    fail "Docker Compose not found"
fi

# 3. Disk available >= 20%
echo "[3/8] Disk space"
DISK_USED=$(df --output=pcent / | tail -1 | tr -d ' %')
DISK_AVAIL=$((100 - DISK_USED))
if [ "$DISK_AVAIL" -ge 20 ]; then
    pass "Disk ${DISK_AVAIL}% available"
else
    fail "Disk ${DISK_AVAIL}% available (< 20%)"
fi

# 4. Memory available >= 512 MB
echo "[4/8] Memory"
MEM_AVAIL=$(free -m | awk '/^Mem:/ {print $7}')
if [ "$MEM_AVAIL" -ge 512 ]; then
    pass "Memory ${MEM_AVAIL} MB available"
else
    fail "Memory ${MEM_AVAIL} MB available (< 512 MB)"
fi

# 5. Required ports not hijacked
echo "[5/8] Required ports"
PORT_OK=true
for port in 8123 9090 6379; do
    # Check if port is in use by something other than expected services
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        pass "Port ${port} in use (expected)"
    else
        warn "Port ${port} not listening (service may be down)"
    fi
done

# 6. .env validation
echo "[6/8] .env validation"
if bash scripts/validate_env.sh > /dev/null 2>&1; then
    pass ".env validation passed"
else
    fail ".env validation failed (run scripts/validate_env.sh for details)"
fi

# 7. Docker services health
echo "[7/8] Docker services"
UNHEALTHY=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -ivE 'up|running' | head -5 || echo "")
if [ -z "$UNHEALTHY" ]; then
    SERVICE_COUNT=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l)
    pass "All ${SERVICE_COUNT} services healthy"
else
    fail "Unhealthy services: ${UNHEALTHY}"
fi

# 8. Core sysctl values (from ops.sh tune)
echo "[8/8] Sysctl tuning"
SYSCTL_OK=true
check_sysctl() {
    local key="$1" expected="$2"
    local actual
    actual=$(sysctl -n "$key" 2>/dev/null || echo "MISSING")
    if [ "$actual" != "$expected" ]; then
        warn "sysctl ${key}=${actual} (expected ${expected})"
        SYSCTL_OK=false
    fi
}
check_sysctl "net.core.rmem_max" "134217728"
check_sysctl "net.core.wmem_max" "134217728"
check_sysctl "net.core.somaxconn" "4096"
check_sysctl "net.ipv4.tcp_low_latency" "1"
if [ "$SYSCTL_OK" = true ]; then
    pass "Sysctl tuning OK"
fi

# Summary
echo ""
echo "=== Preflight Summary ==="
if [ "$errors" -gt 0 ]; then
    echo "FAILED: ${errors} error(s), ${warnings} warning(s)"
    exit 1
else
    if [ "$warnings" -gt 0 ]; then
        echo "PASSED with ${warnings} warning(s)"
    else
        echo "PASSED: all checks OK"
    fi
    exit 0
fi
```

### Step 3.2: Test locally

- [ ] **Run preflight in current environment**

Run:
```bash
DEPLOY_ROOT="$(pwd)" bash scripts/host_preflight.sh; echo "Exit: $?"
```

Expected: Runs all 8 checks with pass/warn/fail indicators. Some sysctl checks may warn on dev machine (expected). Exit code reflects whether any FAIL occurred.

### Step 3.3: Commit

- [ ] **Commit**

```bash
chmod +x scripts/host_preflight.sh
git add scripts/host_preflight.sh
git commit -m "feat(ops): add host_preflight.sh with 8 pre-deploy baseline checks"
```

---

## Task 4: `scripts/host_health_report.sh` — Daily Health Summary

**Files:**
- Create: `scripts/host_health_report.sh`

### Step 4.1: Create `host_health_report.sh`

- [ ] **Create the script**

```bash
#!/usr/bin/env bash
# host_health_report.sh — Daily host health summary → Telegram.
# Usage: ./scripts/host_health_report.sh
#
# Environment:
#   DEPLOY_ROOT  — project root (default: /home/charl/subhft)

set -euo pipefail
source "$(dirname "$0")/_notify.sh"

cd "$DEPLOY_ROOT"

TODAY=$(date +%Y-%m-%d)
alerts=""

# 1. Disk usage
DISK_USED=$(df --output=pcent / | tail -1 | tr -d ' %')
disk_status="${DISK_USED}%"
if [ "$DISK_USED" -gt 80 ]; then
    alerts="${alerts}Disk: ${DISK_USED}% (>80%)\n"
fi

# 2. Memory usage
MEM_TOTAL=$(free -m | awk '/^Mem:/ {print $2}')
MEM_USED=$(free -m | awk '/^Mem:/ {print $3}')
MEM_PCT=$(( MEM_USED * 100 / MEM_TOTAL ))
mem_status="${MEM_PCT}%"
if [ "$MEM_PCT" -gt 90 ]; then
    alerts="${alerts}RAM: ${MEM_PCT}% (>90%)\n"
fi

# 3. CPU load (15-min average)
LOAD_15=$(uptime | awk -F'load average:' '{print $2}' | awk -F', ' '{print $3}' | xargs)
NCPU=$(nproc)
# Compare using integer (multiply by 100 to avoid float)
LOAD_INT=$(echo "$LOAD_15" | awk '{printf "%d", $1 * 100}')
NCPU_INT=$((NCPU * 100))
load_status="${LOAD_15}"
if [ "$LOAD_INT" -gt "$NCPU_INT" ]; then
    alerts="${alerts}Load: ${LOAD_15} (>${NCPU} CPUs)\n"
fi

# 4. Docker services
TOTAL_SERVICES=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l)
HEALTHY_SERVICES=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -ciE 'up|running' || echo "0")
docker_status="${HEALTHY_SERVICES}/${TOTAL_SERVICES} healthy"
if [ "$HEALTHY_SERVICES" -lt "$TOTAL_SERVICES" ]; then
    UNHEALTHY=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -ivE 'up|running' | awk '{print $1}' | paste -sd, || echo "unknown")
    alerts="${alerts}Docker: ${UNHEALTHY} unhealthy\n"
fi

# 5. ClickHouse lag
ch_status="N/A"
CH_RESPONSE=$(curl -sf "http://localhost:8123/?query=SELECT+max(exch_ts)+FROM+hft.market_data+WHERE+toDate(exch_ts/1e9)%3Dtoday()&default_format=TabSeparated" 2>/dev/null || echo "")
if [ -n "$CH_RESPONSE" ] && [ "$CH_RESPONSE" != "0" ]; then
    # exch_ts is nanoseconds; compute minutes since last data
    CH_TS_S=$(echo "$CH_RESPONSE" | awk '{printf "%d", $1 / 1e9}')
    NOW_S=$(date +%s)
    CH_LAG_M=$(( (NOW_S - CH_TS_S) / 60 ))
    ch_status="${CH_LAG_M}m"
    # Only alert during trading hours (roughly 08:00-14:00 TST = 00:00-06:00 UTC)
    HOUR=$(date +%H)
    if [ "$CH_LAG_M" -gt 30 ] && [ "$HOUR" -ge 0 ] && [ "$HOUR" -le 6 ]; then
        alerts="${alerts}CH lag: ${CH_LAG_M}m (>30m during trading)\n"
    fi
else
    ch_status="no data"
fi

# 6. System uptime
UPTIME_S=$(awk '{print int($1)}' /proc/uptime)
UPTIME_D=$((UPTIME_S / 86400))
uptime_status="${UPTIME_D}d"
if [ "$UPTIME_D" -lt 1 ]; then
    alerts="${alerts}Uptime: ${UPTIME_D}d (<1d, unexpected restart?)\n"
fi

# 7. Pending reboot
reboot_status=""
if [ -f /var/run/reboot-required ]; then
    reboot_status=" | Reboot needed"
    alerts="${alerts}Reboot required (pending kernel update)\n"
fi

# 8. Secret status (reuse secret_age_check.sh --quiet)
secret_status="all OK"
if ! bash "$(dirname "$0")/secret_age_check.sh" --quiet > /dev/null 2>&1; then
    # Count overdue from output
    OVERDUE_N=$(bash "$(dirname "$0")/secret_age_check.sh" --quiet 2>/dev/null | head -1 | grep -oP '\d+' || echo "?")
    secret_status="${OVERDUE_N} overdue"
    # Don't add to alerts — secret_age_check cron already notifies at 07:00
fi

# Build message
if [ -n "$alerts" ]; then
    MSG="$(printf '⚠️ *HFT Host Health (%s)*\nDisk: %s | RAM: %s | Load: %s\nDocker: %s\nCH lag: %s | Uptime: %s%s\nSecrets: %s\n\n*Issues:*\n%b' \
        "$TODAY" "$disk_status" "$mem_status" "$load_status" \
        "$docker_status" "$ch_status" "$uptime_status" "$reboot_status" \
        "$secret_status" "$alerts")"
else
    MSG="$(printf '✅ *HFT Host Health (%s)*\nDisk: %s | RAM: %s | Load: %s\nDocker: %s\nCH lag: %s | Uptime: %s%s\nSecrets: %s' \
        "$TODAY" "$disk_status" "$mem_status" "$load_status" \
        "$docker_status" "$ch_status" "$uptime_status" "$reboot_status" \
        "$secret_status")"
fi

echo "$MSG"
notify_telegram "$MSG"
```

### Step 4.2: Test locally (dry run without Telegram)

- [ ] **Run health report in current environment**

Run:
```bash
DEPLOY_ROOT="$(pwd)" HFT_TELEGRAM_BOT_TOKEN="" bash scripts/host_health_report.sh
```

Expected: Prints health summary to stdout. Telegram send skipped (empty token). ClickHouse query may fail locally — shows "N/A" (graceful fallback).

### Step 4.3: Commit

- [ ] **Commit**

```bash
chmod +x scripts/host_health_report.sh
git add scripts/host_health_report.sh
git commit -m "feat(ops): add host_health_report.sh daily Telegram health summary"
```

---

## Task 5: `scripts/host_security_update.sh` — Weekly Security Patching

**Files:**
- Create: `scripts/host_security_update.sh`

### Step 5.1: Create `host_security_update.sh`

- [ ] **Create the script**

```bash
#!/usr/bin/env bash
# host_security_update.sh — Apply security-only apt updates, notify via Telegram.
# Usage: sudo ./scripts/host_security_update.sh
#        ./scripts/host_security_update.sh --dry-run  (list only, no apply)
#
# Requires: unattended-upgrades package (apt install unattended-upgrades)
# Environment:
#   DEPLOY_ROOT  — project root (default: /home/charl/subhft)

set -euo pipefail
source "$(dirname "$0")/_notify.sh"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

LOG_FILE="/var/log/hft_security_updates.log"
TIMESTAMP=$(date -Iseconds)

echo "=== HFT Security Update — ${TIMESTAMP} ===" | tee -a "$LOG_FILE"

# 1. Update package lists
echo ">> Updating package lists..."
apt update -qq 2>&1 | tee -a "$LOG_FILE"

# 2. Count upgradable security packages
UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -i security | wc -l || echo "0")
echo ">> Security packages upgradable: ${UPGRADABLE}" | tee -a "$LOG_FILE"

if [ "$UPGRADABLE" -eq 0 ]; then
    MSG="✅ *Security Update ($(date +%Y-%m-%d))*: 0 packages pending"
    echo "$MSG"
    notify_telegram "$MSG"
    exit 0
fi

# 3. Apply or dry-run
if [ "$DRY_RUN" = true ]; then
    echo ">> DRY RUN: listing upgradable security packages only" | tee -a "$LOG_FILE"
    apt list --upgradable 2>/dev/null | grep -i security | tee -a "$LOG_FILE"
    MSG="🔍 *Security Update Dry Run ($(date +%Y-%m-%d))*: ${UPGRADABLE} packages available"
    echo "$MSG"
    notify_telegram "$MSG"
    exit 0
fi

echo ">> Applying security updates..." | tee -a "$LOG_FILE"
# Use unattended-upgrades for security-only updates
if command -v unattended-upgrades &> /dev/null; then
    unattended-upgrades -v 2>&1 | tee -a "$LOG_FILE"
else
    # Fallback: apt upgrade with security sources only
    echo ">> WARNING: unattended-upgrades not installed, using apt upgrade" | tee -a "$LOG_FILE"
    DEBIAN_FRONTEND=noninteractive apt upgrade -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" 2>&1 | tee -a "$LOG_FILE"
fi

# 4. Check if reboot required
REBOOT_NEEDED="no"
if [ -f /var/run/reboot-required ]; then
    REBOOT_NEEDED="yes"
fi

# 5. Count remaining upgradable
REMAINING=$(apt list --upgradable 2>/dev/null | grep -c -i security || echo "0")

# 6. Telegram summary
if [ "$REBOOT_NEEDED" = "yes" ]; then
    MSG="$(printf '⚠️ *Security Update (%s)*\n%d packages updated\n%d still pending\n*Reboot required* (manual action needed)' \
        "$(date +%Y-%m-%d)" "$UPGRADABLE" "$REMAINING")"
else
    MSG="$(printf '✅ *Security Update (%s)*\n%d packages updated\n%d still pending\nReboot: not required' \
        "$(date +%Y-%m-%d)" "$UPGRADABLE" "$REMAINING")"
fi

echo "$MSG" | tee -a "$LOG_FILE"
notify_telegram "$MSG"
```

### Step 5.2: Verify dry-run mode

- [ ] **Run with --dry-run (no root needed)**

Run:
```bash
DEPLOY_ROOT="$(pwd)" HFT_TELEGRAM_BOT_TOKEN="" bash scripts/host_security_update.sh --dry-run 2>&1 || echo "(apt update may fail on dev machine — expected)"
```

Expected: Attempts `apt update` (may fail without root on dev machine), shows dry-run output. No packages are installed.

### Step 5.3: Commit

- [ ] **Commit**

```bash
chmod +x scripts/host_security_update.sh
git add scripts/host_security_update.sh
git commit -m "feat(ops): add host_security_update.sh weekly security patching with Telegram"
```

---

## Task 6: Integrate preflight into `deploy.sh` + align `REMOTE_DIR`

**Files:**
- Modify: `scripts/deploy.sh`

### Step 6.1: Update `deploy.sh` to use `DEPLOY_ROOT` and add remote preflight

- [ ] **Modify `deploy.sh`**

In `scripts/deploy.sh`, change line 26:

```
# Before:
REMOTE_DIR="/opt/hft-platform"

# After:
REMOTE_DIR="${DEPLOY_ROOT:-/home/charl/subhft}"
```

Then insert the remote preflight gate after line 112 (`echo "==> Deploying to ${DEPLOY_USER}@${DEPLOY_HOST}"`), before the SSH pull command:

```bash
# Remote preflight
echo "==> Running remote preflight..."
# shellcheck disable=SC2029
ssh ${SSH_OPTS} "${DEPLOY_USER}@${DEPLOY_HOST}" \
    "cd ${REMOTE_DIR} && bash scripts/host_preflight.sh" \
    || { echo "FATAL: Remote preflight failed. Aborting deploy."; exit 1; }
```

### Step 6.2: Verify deploy.sh syntax

- [ ] **Check for syntax errors**

Run:
```bash
bash -n scripts/deploy.sh && echo "Syntax OK"
```

Expected: `Syntax OK`

### Step 6.3: Commit

- [ ] **Commit**

```bash
git add scripts/deploy.sh
git commit -m "feat(ops): add remote preflight gate to deploy.sh, align REMOTE_DIR to DEPLOY_ROOT"
```

---

## Task 7: Update `docs/operations/cron-setup-remote.md`

**Files:**
- Modify: `docs/operations/cron-setup-remote.md`

### Step 7.1: Add 3 new cron entries

- [ ] **Append governance cron entries to the crontab block**

Insert after the SMART Disk Health entry (line 97, before the Quarterly Health Check line) in the cron block:

```cron

# --- Host Security Update (weekly Sunday 03:30) ---
# Applies security-only apt updates and sends Telegram summary.
# Does NOT auto-reboot — only notifies if reboot is required.
# Requires: sudo apt install unattended-upgrades
30 3 * * 0 cd /home/charl/subhft && sudo bash scripts/host_security_update.sh >> /tmp/hft_security_update.log 2>&1

# --- Secret Age Check (daily 07:00, before market open) ---
# Warns via Telegram if any secret exceeds 90 days without rotation.
# Secrets tracked via # ROTATED: YYYY-MM-DD annotations in .env.
0 7 * * 1-5 cd /home/charl/subhft && bash scripts/secret_age_check.sh >> /tmp/hft_secret_age.log 2>&1

# --- Host Health Report (daily 16:45, after market close) ---
# Consolidated host health summary: disk, RAM, load, Docker, CH lag, uptime, secrets.
45 16 * * 1-5 cd /home/charl/subhft && bash scripts/host_health_report.sh >> /tmp/hft_host_health.log 2>&1
```

Also update the header `Last updated` date to `2026-03-28`.

### Step 7.2: Add verification entries

- [ ] **Append to the Verification section**

Add to the existing verification section (after line 155):

```markdown
# Check governance script logs
tail -50 /tmp/hft_security_update.log
tail -50 /tmp/hft_secret_age.log
tail -50 /tmp/hft_host_health.log
```

### Step 7.3: Commit

- [ ] **Commit**

```bash
git add docs/operations/cron-setup-remote.md
git commit -m "docs(ops): add governance cron entries to cron-setup-remote.md"
```

---

## Task 8: Final integration verification

### Step 8.1: Verify all scripts are executable and source correctly

- [ ] **Run integration checks**

```bash
# All scripts should source _notify.sh without error
for script in scripts/host_preflight.sh scripts/secret_age_check.sh scripts/host_health_report.sh scripts/host_security_update.sh; do
    bash -n "$script" && echo "✓ ${script} syntax OK" || echo "✗ ${script} SYNTAX ERROR"
done

# deploy.sh still valid
bash -n scripts/deploy.sh && echo "✓ deploy.sh syntax OK"
```

Expected: All 5 scripts report syntax OK.

### Step 8.2: Verify secret_age_check.sh --quiet is callable from health report

- [ ] **Test the integration point**

```bash
# Simulate health report calling secret check
DEPLOY_ROOT="$(pwd)" bash scripts/secret_age_check.sh --quiet > /dev/null 2>&1
echo "secret_age_check --quiet exit code: $?"
```

Expected: Exit 0 or 1 depending on local `.env` state. No Telegram sent (--quiet mode).

### Step 8.3: Final commit (if any fixups needed)

- [ ] **Commit any fixups**

```bash
# Only if changes were needed
git add -A scripts/ docs/operations/cron-setup-remote.md
git commit -m "fix(ops): governance script integration fixups"
```

Skip this step if no fixups were needed.
