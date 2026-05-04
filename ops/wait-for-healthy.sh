#!/usr/bin/env bash
# Wait for engine to become healthy after start
set -euo pipefail

MAX_WAIT=120
INTERVAL=5
ELAPSED=0

# P2 fix (2026-05-04): default to the project-local bind-mount path written
# by services/heartbeat.py via /var/run/hft/heartbeat inside the container.
# Override with HFT_HEARTBEAT_FILE for non-default deploy layouts.
HEARTBEAT_FILE="${HFT_HEARTBEAT_FILE:-./.hft-runtime/heartbeat}"

echo "[wait-for-healthy] Waiting up to ${MAX_WAIT}s for engine health..."

while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    if [ -f "$HEARTBEAT_FILE" ]; then
        AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") ))
        if [ "$AGE" -lt 60 ]; then
            echo "[wait-for-healthy] Engine healthy (heartbeat age: ${AGE}s)"
            exit 0
        fi
    fi
    sleep "$INTERVAL"
    ELAPSED=$((ELAPSED + INTERVAL))
done

echo "[wait-for-healthy] WARNING: Engine did not become healthy within ${MAX_WAIT}s"
exit 1
