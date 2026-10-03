#!/usr/bin/env bash
# Daily ClickHouse backup — intended for cron invocation.
# Usage: ./scripts/clickhouse_backup.sh
#
# Requires: HFT_BACKUP_ENABLED=1 in environment
# Schedule: 30 14 * * * /opt/hft/scripts/clickhouse_backup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Run inside hft-engine container where clickhouse_driver is installed.
# Pass through HFT_BACKUP_ENABLED from host env (crontab sets it).
# Retention is set here, not in the container env: `docker exec -e` overrides
# the creation-time value, so changing it needs no container recreate. 3 daily
# backups (~12.5G each) because they share a disk with the data (2026-10-03).
exec docker compose exec -T \
    -e HFT_BACKUP_ENABLED="${HFT_BACKUP_ENABLED:-1}" \
    -e HFT_BACKUP_RETAIN_DAYS="${HFT_BACKUP_RETAIN_DAYS:-3}" \
    hft-engine python -c "
from hft_platform.ops.backup import BackupManager
import sys

mgr = BackupManager()
success = mgr.run_daily()
sys.exit(0 if success else 1)
"
