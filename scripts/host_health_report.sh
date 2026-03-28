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

# 8. Secret status (reuse secret_age_check.sh --quiet, single invocation)
secret_status="all OK"
secret_output=$(bash "$(dirname "$0")/secret_age_check.sh" --quiet 2>&1) || secret_rc=$?
secret_rc=${secret_rc:-0}
if [ "$secret_rc" -ne 0 ]; then
    OVERDUE_N=$(echo "$secret_output" | head -1 | grep -oP '\d+(?= issue)' || echo "?")
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
