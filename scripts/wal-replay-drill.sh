#!/usr/bin/env bash
# wal-replay-drill.sh — Operational drill: WAL accumulation during ClickHouse outage + replay
#
# Usage: ./scripts/wal-replay-drill.sh
#
# Prerequisites:
#   - Docker Compose stack running (make start)
#   - ClickHouse healthy (port 8123)
#   - WAL mode enabled (HFT_RECORDER_MODE=wal_first)

set -euo pipefail

CLICKHOUSE_CONTAINER="${CLICKHOUSE_CONTAINER:-clickhouse}"
CLICKHOUSE_HTTP="${CLICKHOUSE_HTTP:-http://localhost:8123}"
WAL_LOADER_SERVICE="${WAL_LOADER_SERVICE:-wal-loader}"

echo "============================================"
echo "  WAL Replay Drill — Operational Playbook"
echo "============================================"
echo ""

# ── Step 1: Record baseline ClickHouse row count ──────────────────────
echo "[Step 1/6] Recording baseline ClickHouse row count..."
BASELINE=$(curl -s "${CLICKHOUSE_HTTP}" \
    --data-binary "SELECT count() FROM hft.market_data" 2>/dev/null || echo "0")
BASELINE="${BASELINE//[^0-9]/}"
BASELINE="${BASELINE:-0}"
echo "  Baseline row count: ${BASELINE}"
echo ""

# ── Step 2: Stop ClickHouse (simulate outage) ────────────────────────
echo "[Step 2/6] Stopping ClickHouse container (simulating outage)..."
docker compose stop "${CLICKHOUSE_CONTAINER}"
echo "  ClickHouse stopped."
echo ""

# ── Step 3: Wait for WAL accumulation ────────────────────────────────
echo "[Step 3/6] WAL is now accumulating while ClickHouse is down."
echo "  Let the HFT engine run for a while to generate WAL files."
echo ""
read -r -p "  Press ENTER when ready to proceed with replay... "
echo ""

# ── Step 4: Restart ClickHouse ───────────────────────────────────────
echo "[Step 4/6] Restarting ClickHouse..."
docker compose start "${CLICKHOUSE_CONTAINER}"
echo "  Waiting for ClickHouse to become healthy..."
for i in $(seq 1 30); do
    if curl -sf "${CLICKHOUSE_HTTP}" --data-binary "SELECT 1" >/dev/null 2>&1; then
        echo "  ClickHouse is healthy (attempt ${i})."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ERROR: ClickHouse did not become healthy in 30 attempts."
        exit 1
    fi
    sleep 2
done
echo ""

# ── Step 5: Run WAL loader replay ────────────────────────────────────
echo "[Step 5/6] Running WAL loader replay..."
docker compose run --rm "${WAL_LOADER_SERVICE}"
echo "  WAL replay completed."
echo ""

# ── Step 6: Verify row count ────────────────────────────────────────
echo "[Step 6/6] Verifying row count after replay..."
AFTER=$(curl -s "${CLICKHOUSE_HTTP}" \
    --data-binary "SELECT count() FROM hft.market_data" 2>/dev/null || echo "0")
AFTER="${AFTER//[^0-9]/}"
AFTER="${AFTER:-0}"
DELTA=$((AFTER - BASELINE))

echo "  Baseline:  ${BASELINE}"
echo "  After:     ${AFTER}"
echo "  Delta:     ${DELTA}"
echo ""

if [ "${DELTA}" -gt 0 ]; then
    echo "  RESULT: PASS — ${DELTA} rows recovered from WAL replay."
else
    echo "  RESULT: FAIL — No new rows after replay (delta=${DELTA})."
    echo "  Check WAL directory and loader logs for errors."
    exit 1
fi

echo ""
echo "============================================"
echo "  WAL Replay Drill Complete"
echo "============================================"
