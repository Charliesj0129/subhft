#!/usr/bin/env bash
# Validates .env consistency before deployment.
# Usage: ./scripts/validate_env.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "FATAL: .env not found. Copy from .env.example and configure."
    exit 1
fi

source .env
errors=0

# 1. CLICKHOUSE_PASSWORD must not be empty
if [ -z "${CLICKHOUSE_PASSWORD:-}" ]; then
    echo "ERROR: CLICKHOUSE_PASSWORD is empty"
    errors=$((errors+1))
fi

# 2. REDIS_PASSWORD must not be empty
if [ -z "${REDIS_PASSWORD:-}" ]; then
    echo "ERROR: REDIS_PASSWORD is empty"
    errors=$((errors+1))
fi

# 3. HFT_CLICKHOUSE_PORT must be 8123 (HTTP, for clickhouse_connect)
if [ "${HFT_CLICKHOUSE_PORT:-8123}" != "8123" ]; then
    echo "WARNING: HFT_CLICKHOUSE_PORT=${HFT_CLICKHOUSE_PORT} — expected 8123 (HTTP). Port 9000 is native protocol."
fi

# 4. MONITOR passwords should match infra passwords
if [ -n "${MONITOR_CH_PASSWORD:-}" ] && [ "${MONITOR_CH_PASSWORD}" != "${CLICKHOUSE_PASSWORD}" ]; then
    echo "ERROR: MONITOR_CH_PASSWORD (${MONITOR_CH_PASSWORD}) != CLICKHOUSE_PASSWORD (${CLICKHOUSE_PASSWORD})"
    errors=$((errors+1))
fi
if [ -n "${MONITOR_REDIS_PASSWORD:-}" ] && [ "${MONITOR_REDIS_PASSWORD}" != "${REDIS_PASSWORD}" ]; then
    echo "ERROR: MONITOR_REDIS_PASSWORD (${MONITOR_REDIS_PASSWORD}) != REDIS_PASSWORD (${REDIS_PASSWORD})"
    errors=$((errors+1))
fi

# 5. Shioaji credentials for real mode
if [ "${HFT_MODE:-sim}" = "real" ]; then
    if [ -z "${SHIOAJI_API_KEY:-}" ]; then
        echo "ERROR: HFT_MODE=real but SHIOAJI_API_KEY is empty"
        errors=$((errors+1))
    fi
    if [ -z "${SHIOAJI_SECRET_KEY:-}" ]; then
        echo "ERROR: HFT_MODE=real but SHIOAJI_SECRET_KEY is empty"
        errors=$((errors+1))
    fi
fi

# 6. Telegram for production
if [ "${HFT_TELEGRAM_ENABLED:-0}" = "1" ]; then
    if [ -z "${HFT_TELEGRAM_BOT_TOKEN:-}" ]; then
        echo "ERROR: HFT_TELEGRAM_ENABLED=1 but BOT_TOKEN is empty"
        errors=$((errors+1))
    fi
    if [ -z "${HFT_TELEGRAM_CHAT_ID:-}" ]; then
        echo "ERROR: HFT_TELEGRAM_ENABLED=1 but CHAT_ID is empty"
        errors=$((errors+1))
    fi
fi

# 7. No duplicate lines
dupes=$(sort .env | grep -v "^#" | grep -v "^$" | cut -d= -f1 | uniq -d)
if [ -n "$dupes" ]; then
    echo "WARNING: Duplicate keys in .env: $dupes"
fi

if [ $errors -gt 0 ]; then
    echo ""
    echo "FAILED: $errors error(s) found. Fix .env before deploying."
    exit 1
else
    echo "PASSED: .env validation OK"
    exit 0
fi
