#!/usr/bin/env bash
# Disaster recovery: restore ClickHouse database from backup.
# Usage: ./scripts/clickhouse_restore.sh <backup_name>
# Example: ./scripts/clickhouse_restore.sh daily_20260325
# NOTE: requires interactive TTY for confirmation prompt
set -euo pipefail

BACKUP_NAME="${1:?Usage: $0 <backup_name>}"

echo "Restoring ClickHouse database 'hft' from backup '${BACKUP_NAME}'..."
echo "WARNING: This will overwrite existing data in the 'hft' database."
read -r -p "Continue? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 1
fi

docker exec clickhouse clickhouse-client \
  --query "RESTORE DATABASE hft FROM Disk('backup_local', '${BACKUP_NAME}/')"

echo "Restore complete. Verify with: docker exec clickhouse clickhouse-client --query 'SELECT count() FROM hft.market_data'"
