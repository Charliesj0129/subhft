#!/usr/bin/env bash
# Quarterly Chaos Drill — runs all 5 chaos playbooks with timing and report.
#
# Usage: ./scripts/run-chaos-drill.sh
# Output: Console report + log file at /tmp/chaos-drill-YYYYMMDD.log
set -euo pipefail

DATE=$(date +%Y%m%d)
LOG="/tmp/chaos-drill-${DATE}.log"

echo "=== Quarterly Chaos Drill ===" | tee "$LOG"
echo "Date: $(date -Iseconds)" | tee -a "$LOG"
echo "Operator: $(whoami)" | tee -a "$LOG"
echo "" | tee -a "$LOG"

PASS=0
FAIL=0

for playbook in broker_disconnect clickhouse_down feed_gap position_drift disk_full; do
    echo "--- Playbook: ${playbook} ---" | tee -a "$LOG"
    if uv run pytest "tests/chaos/test_playbook_${playbook}.py" -v --no-cov --tb=short 2>&1 | tee -a "$LOG"; then
        echo "  Result: PASS" | tee -a "$LOG"
        PASS=$((PASS + 1))
    else
        echo "  Result: FAIL" | tee -a "$LOG"
        FAIL=$((FAIL + 1))
    fi
    echo "" | tee -a "$LOG"
done

echo "=== Summary ===" | tee -a "$LOG"
echo "Passed: ${PASS}/5" | tee -a "$LOG"
echo "Failed: ${FAIL}/5" | tee -a "$LOG"

if [ "$FAIL" -eq 0 ]; then
    echo "DRILL RESULT: ALL PASS" | tee -a "$LOG"
else
    echo "DRILL RESULT: ${FAIL} FAILURES — review log at ${LOG}" | tee -a "$LOG"
    exit 1
fi

echo "" | tee -a "$LOG"
echo "Log saved to: ${LOG}" | tee -a "$LOG"
echo "=== Drill Complete ==="
