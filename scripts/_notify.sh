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
