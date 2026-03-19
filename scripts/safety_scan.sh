#!/usr/bin/env bash
set -euo pipefail
SRC="src/hft_platform"; FAIL=0; MAX=3
BC=$(grep -r --include='*.py' -c 'except Exception:$' "$SRC" 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
[ "$BC" -gt "$MAX" ] && echo "FAIL: bare except=$BC" && FAIL=1 || echo "OK: bare except=$BC"
echo ""; [ "$FAIL" -ne 0 ] && echo "SAFETY SCAN: FAILED" && exit 1 || echo "SAFETY SCAN: PASSED"
