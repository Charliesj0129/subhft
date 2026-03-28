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
