#!/usr/bin/env bash
# _notify.sh — Shared helper for HFT governance scripts.
# Source this file; do not execute directly.
# Provides: DEPLOY_ROOT, SCRIPT_DIR, notify_telegram()

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT="${DEPLOY_ROOT:-/home/charl/subhft}"

# Load .env for Telegram credentials (if present)
# Uses read+case instead of eval to prevent command injection from .env values
if [ -f "${DEPLOY_ROOT}/.env" ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            HFT_TELEGRAM_BOT_TOKEN) export HFT_TELEGRAM_BOT_TOKEN="${value%%#*}" ;;
            HFT_TELEGRAM_CHAT_ID)   export HFT_TELEGRAM_CHAT_ID="${value%%#*}" ;;
        esac
    done < <(grep -E '^HFT_TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "${DEPLOY_ROOT}/.env")
fi

# notify_telegram MESSAGE
# Returns 0 whether or not the message was delivered, so a failed notification never aborts a caller running under
# `set -e`. It no longer fails silently: a missing credential or a failed send is written to stderr (never the token
# or the URL). Set NOTIFY_STRICT=1 to make those cases return non-zero.
notify_telegram() {
    local msg="$1"
    if [ -z "${HFT_TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${HFT_TELEGRAM_CHAT_ID:-}" ]; then
        local hint=""
        [ -f "${DEPLOY_ROOT}/.env" ] || hint=" (no .env at ${DEPLOY_ROOT})"
        echo "notify_telegram: HFT_TELEGRAM_BOT_TOKEN / HFT_TELEGRAM_CHAT_ID not set${hint}; notification NOT sent" >&2
        [ "${NOTIFY_STRICT:-0}" = "1" ] && return 1
        return 0
    fi
    local code="" rc=0
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
        "https://api.telegram.org/bot${HFT_TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${HFT_TELEGRAM_CHAT_ID}" \
        -d parse_mode=Markdown \
        -d text="${msg}" 2> /dev/null)" || rc=$?
    if [ "$rc" -ne 0 ] || [ "$code" != "200" ]; then
        echo "notify_telegram: send failed (curl exit ${rc}, HTTP ${code:-none}); notification NOT delivered" >&2
        [ "${NOTIFY_STRICT:-0}" = "1" ] && return 1
    fi
    return 0
}
