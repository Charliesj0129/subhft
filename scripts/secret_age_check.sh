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
