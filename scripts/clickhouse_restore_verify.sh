#!/usr/bin/env bash
# Restore verification: restore to temp DB, compare row counts, drop temp DB.
# Usage: ./scripts/clickhouse_restore_verify.sh <backup_name>
# Example: ./scripts/clickhouse_restore_verify.sh daily_20260325
set -euo pipefail

BACKUP_NAME="${1:?Usage: $0 <backup_name>}"
TEMP_DB="hft_restore_test"
CH="docker exec clickhouse clickhouse-client --query"

echo "Restoring backup '${BACKUP_NAME}' to temp database '${TEMP_DB}'..."
$CH "RESTORE DATABASE hft AS ${TEMP_DB} FROM Disk('backup_local', '${BACKUP_NAME}/')"

echo ""
echo "Comparing row counts:"
echo "-------------------------------------------"

TABLES=$($CH "SELECT name FROM system.tables WHERE database = 'hft'" | tr '\n' ' ')

ALL_MATCH=true
for TABLE in $TABLES; do
    ORIG=$($CH "SELECT count() FROM hft.${TABLE}")
    RESTORED=$($CH "SELECT count() FROM ${TEMP_DB}.${TABLE}")
    if [ "$ORIG" = "$RESTORED" ]; then
        STATUS="OK"
    else
        STATUS="MISMATCH"
        ALL_MATCH=false
    fi
    printf "%-30s orig=%-10s restored=%-10s %s\n" "$TABLE" "$ORIG" "$RESTORED" "$STATUS"
done

echo "-------------------------------------------"
echo "Dropping temp database '${TEMP_DB}'..."
$CH "DROP DATABASE IF EXISTS ${TEMP_DB}"

if [ "$ALL_MATCH" = true ]; then
    echo "PASS: All tables match."
    exit 0
else
    echo "FAIL: Row count mismatches detected."
    exit 1
fi
