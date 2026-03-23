#!/usr/bin/env bash
# Watchdog: restart engine if heartbeat is stale
set -euo pipefail

HEARTBEAT_FILE="/tmp/hft-heartbeat"
MAX_AGE=90
SERVICE="hft-engine"

if [ ! -f "$HEARTBEAT_FILE" ]; then
    echo "[watchdog] Heartbeat file missing — restarting $SERVICE"
    systemctl restart "$SERVICE" 2>/dev/null || true
    exit 1
fi

AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") ))

if [ "$AGE" -gt "$MAX_AGE" ]; then
    echo "[watchdog] Heartbeat stale (${AGE}s > ${MAX_AGE}s) — restarting $SERVICE"
    systemctl restart "$SERVICE" 2>/dev/null || true
    exit 1
fi

echo "[watchdog] Heartbeat OK (age: ${AGE}s)"
exit 0
