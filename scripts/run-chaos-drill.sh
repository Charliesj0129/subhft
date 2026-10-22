#!/usr/bin/env bash
# Quarterly Chaos Drill — runs all 5 chaos playbooks with timing and report.
#
# Usage: ./scripts/run-chaos-drill.sh   (from any directory)
# Output: Console report + log file at <repo>/outputs/drills/chaos-drill-YYYYMMDD.log.
#         outputs/ is untracked local storage, not /tmp, so the sign-off evidence outlives a host clean-up.
#         A second run on the same day appends to the same log; a failed run's record is never replaced.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DATE=$(date +%Y%m%d)
LOG_DIR="${REPO_ROOT}/outputs/drills"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/chaos-drill-${DATE}.log"

echo "=== Quarterly Chaos Drill ===" | tee -a "$LOG"
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
