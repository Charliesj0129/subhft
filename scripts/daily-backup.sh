#!/usr/bin/env bash
# Run daily ClickHouse backup via BackupManager.
# Crontab: 0 17 * * 1-5 /path/to/scripts/daily-backup.sh
#
# Prerequisites:
#   - HFT_CLICKHOUSE_HOST, HFT_CLICKHOUSE_PORT env vars set
#   - ClickHouse backup disk configured (see ops docs)
#   - HFT_BACKUP_ENABLED=1
#
# Env vars:
#   HFT_BACKUP_RETAIN_DAYS  — retention period (default: 30)
#   HFT_CLICKHOUSE_HOST     — ClickHouse host (default: localhost)
#   HFT_CLICKHOUSE_PORT     — ClickHouse native port (default: 9000)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "[$(date -Iseconds)] Starting daily ClickHouse backup..."

export HFT_BACKUP_ENABLED=1

uv run python -c "
import asyncio
from hft_platform.ops.backup import BackupManager

async def main():
    mgr = BackupManager()
    await mgr.run_daily()

asyncio.run(main())
"

echo "[$(date -Iseconds)] Daily backup complete."
