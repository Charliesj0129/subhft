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
rc=0
docker compose exec -T \
    -e HFT_BACKUP_ENABLED="${HFT_BACKUP_ENABLED:-1}" \
    -e HFT_BACKUP_RETAIN_DAYS="${HFT_BACKUP_RETAIN_DAYS:-3}" \
    hft-engine python -c "
from hft_platform.ops.backup import BackupManager
import sys

mgr = BackupManager()
success = mgr.run_daily()
sys.exit(0 if success else 1)
" || rc=$?

# Export the outcome through node-exporter's textfile collector (scraped as
# job="node"). The gauge BackupManager sets lives in the one-shot exec above and
# never reaches a scrape target, so BackupStale used to be gated off, and the
# backup failed 2026-10-13..21 unseen. Written after every run, success or not;
# a failure keeps the previous success time (0 when none is known).
TEXTFILE_DIR="${HFT_BACKUP_TEXTFILE_DIR:-/var/lib/node-exporter/textfile}"
PROM_FILE="$TEXTFILE_DIR/hft_backup.prom"
now="$(date +%s)"
last_success="$(awk '$1 == "hft_backup_cron_last_success_timestamp_seconds" {print $2}' "$PROM_FILE" 2>/dev/null || true)"
if [ "$rc" -eq 0 ]; then
    last_success="$now"
fi
if [ -d "$TEXTFILE_DIR" ] && [ -w "$TEXTFILE_DIR" ]; then
    # The collector reads only *.prom, so the temp file is never half-read.
    tmp="$(mktemp "$TEXTFILE_DIR/.hft_backup.XXXXXX")"
    cat > "$tmp" <<PROM
# HELP hft_backup_cron_last_run_timestamp_seconds Unix time the daily backup cron last finished.
# TYPE hft_backup_cron_last_run_timestamp_seconds gauge
hft_backup_cron_last_run_timestamp_seconds $now
# HELP hft_backup_cron_last_exit_code Exit code of the last daily backup run (0 = success).
# TYPE hft_backup_cron_last_exit_code gauge
hft_backup_cron_last_exit_code $rc
# HELP hft_backup_cron_last_success_timestamp_seconds Unix time of the last successful daily backup (0 = none known).
# TYPE hft_backup_cron_last_success_timestamp_seconds gauge
hft_backup_cron_last_success_timestamp_seconds ${last_success:-0}
PROM
    chmod 644 "$tmp"
    mv -f "$tmp" "$PROM_FILE"
else
    echo "WARN: $TEXTFILE_DIR is not writable; backup outcome (exit $rc) not exported" >&2
fi
exit "$rc"
