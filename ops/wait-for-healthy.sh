#!/usr/bin/env bash
# Wait for engine to become healthy after start
set -euo pipefail

MAX_WAIT=120
INTERVAL=5
ELAPSED=0

echo "[wait-for-healthy] Waiting up to ${MAX_WAIT}s for engine health..."

while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    if [ -f /tmp/hft-heartbeat ]; then
        AGE=$(( $(date +%s) - $(stat -c %Y /tmp/hft-heartbeat) ))
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
