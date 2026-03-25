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

exec uv run python -c "
from hft_platform.ops.backup import BackupManager
import sys

mgr = BackupManager()
success = mgr.run_daily()
sys.exit(0 if success else 1)
"
