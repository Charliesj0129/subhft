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

# Resolve uv path — cron/non-interactive shells lack ~/.local/bin in PATH
UV="${HOME}/.local/bin/uv"
if ! command -v uv &>/dev/null && [ -x "$UV" ]; then
    :
elif command -v uv &>/dev/null; then
    UV="uv"
else
    echo "ERROR: uv not found at $UV or in PATH" >&2
    exit 1
fi

exec "$UV" run python -c "
from hft_platform.ops.backup import BackupManager
import sys

mgr = BackupManager()
success = mgr.run_daily()
sys.exit(0 if success else 1)
"
