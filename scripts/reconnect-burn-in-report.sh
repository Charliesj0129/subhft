#!/usr/bin/env bash
# Reconnect Burn-In Report — queries Prometheus for reconnect metrics.
#
# Usage: ./scripts/reconnect-burn-in-report.sh [PROMETHEUS_URL]
# Default: http://localhost:9091
set -euo pipefail

PROM="${1:-http://localhost:9091}"

echo "=== Reconnect Burn-In Report ==="
echo "Date: $(date -Iseconds)"
echo "Prometheus: ${PROM}"
echo ""

echo "--- Reconnect Totals (last 5 days) ---"
curl -s "${PROM}/api/v1/query?query=sum(feed_reconnect_total)%20by%20(result)" 2>/dev/null | \
    python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for r in data.get('data', {}).get('result', []):
        label = r['metric'].get('result', 'unknown')
        value = r['value'][1]
        print(f'  {label}: {value}')
except Exception as e:
    print(f'  Query failed: {e}')
" || echo "  Prometheus not reachable"

echo ""
echo "--- Reconnect Timeouts ---"
curl -s "${PROM}/api/v1/query?query=sum(feed_reconnect_timeout_total)" 2>/dev/null | \
    python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for r in data.get('data', {}).get('result', []):
        print(f'  Timeouts: {r[\"value\"][1]}')
except Exception as e:
    print(f'  Query failed: {e}')
" || echo "  Prometheus not reachable"

echo ""
echo "=== End Report ==="
