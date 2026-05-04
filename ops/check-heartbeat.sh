#!/usr/bin/env bash
# Watchdog: restart engine if heartbeat is stale
set -euo pipefail

# P2 fix (2026-05-04): default to the project-local bind-mount path written
# by services/heartbeat.py via /var/run/hft/heartbeat inside the container.
# Override with HFT_HEARTBEAT_FILE for non-default deploy layouts.
HEARTBEAT_FILE="${HFT_HEARTBEAT_FILE:-./.hft-runtime/heartbeat}"
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
