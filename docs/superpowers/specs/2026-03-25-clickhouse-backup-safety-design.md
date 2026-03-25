# ClickHouse Backup & Data Safety Layer — Design Spec

**Date**: 2026-03-25
**Status**: Implemented
**Scope**: Sub-project A of Long-Term Operations Readiness

## Problem Statement

The HFT platform has **zero ClickHouse backup automation**. If the ClickHouse Docker volume is corrupted, lost, or accidentally deleted, all historical market data, fills, and shadow orders are irrecoverably gone. For a platform targeting unattended 24h operation, this is the highest-risk gap.

## Goals

1. **Automated daily backup** of all ClickHouse data (database `hft`) to local disk
2. **30-day retention** with automatic cleanup of old backups
3. **Verified backups** — every backup is validated before being considered successful
4. **One-command restore** for disaster recovery
5. **Observability** — Prometheus metrics + Telegram alerts for backup status
6. **Archive hook** — reserved interface for future cold archival (not implemented)

## Non-Goals

- Remote/offsite backup (S3, NAS) — future enhancement
- ClickHouse HA / replication — separate project
- Cold archival implementation — only the hook point
- Incremental backups — not needed at <100GB scale

## Constraints

- ClickHouse version: `clickhouse/clickhouse-server:25.12.3` (supports native `BACKUP/RESTORE`)
- Single Docker node, data <100GB
- Backup destination: local disk (separate Docker volume)
- Must not interfere with trading hours (TWSE 09:00-13:30, TAIFEX day 08:45-13:45, night 15:00-05:00)

## Architecture

### Overview

```
Daily Cron (14:30 TWD)
  → BackupManager.run_daily()
    → BACKUP DATABASE hft TO Disk('backup_local', 'daily_YYYYMMDD/')
    → Verify: system.backups status + directory size check
    → Cleanup: remove backups older than 30 days
    → Archive hook: _run_archive_hook() (no-op)
    → Notify: Telegram success/failure via NotificationDispatcher
    → Metrics: update Prometheus gauges
```

### Components

| Component | File | Responsibility |
|-----------|------|----------------|
| ClickHouse backup disk config | `config/clickhouse_backup.xml` | Declare `backup_local` disk in ClickHouse |
| BackupManager | `src/hft_platform/ops/backup.py` | Orchestrate backup/verify/cleanup/notify |
| Backup shell entry | `scripts/clickhouse_backup.sh` | Cron-friendly wrapper for BackupManager |
| Restore script | `scripts/clickhouse_restore.sh` | Manual disaster recovery entry point |
| Notification extension | `src/hft_platform/notifications/dispatcher.py` | `notify_backup_success`, `notify_backup_failed` |
| Template extension | `src/hft_platform/notifications/templates.py` | `render_backup_success`, `render_backup_failed` |
| Metrics extension | `src/hft_platform/observability/metrics.py` | 4 new Gauge metrics |
| Docker volume | `docker-compose.yml` | Mount backup volume + config file |

## Detailed Design

### 1. ClickHouse Backup Disk Configuration

New file: `config/clickhouse_backup.xml`

```xml
<clickhouse>
  <storage_configuration>
    <disks>
      <backup_local>
        <type>local</type>
        <path>/backups/</path>
      </backup_local>
    </disks>
  </storage_configuration>
  <backups>
    <allowed_disk>backup_local</allowed_disk>
  </backups>
</clickhouse>
```

Docker volume mount added to `docker-compose.yml` clickhouse service:

```yaml
volumes:
  - ./config/clickhouse_backup.xml:/etc/clickhouse-server/config.d/backup.xml:ro
  - ${CH_BACKUP_PATH:-./backups/clickhouse}:/backups
```

### 2. BackupManager

`src/hft_platform/ops/backup.py`

```python
class BackupManager:
    __slots__ = ("_ch_host", "_ch_port", "_ch_user", "_ch_password",
                 "_retain_days", "_backup_dir", "_notifier", "_metrics")

    def __init__(
        self,
        retain_days: int = 30,
        backup_dir: str = "/backups",
        notifier: NotificationDispatcher | None = None,
        ch_host: str | None = None,   # default: os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
        ch_port: int | None = None,    # default: int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
        ch_user: str | None = None,    # default: os.getenv("HFT_CLICKHOUSE_USER", "default")
        ch_password: str | None = None,  # default: os.getenv("HFT_CLICKHOUSE_PASSWORD", "")
    ) -> None: ...

    def run_daily(self) -> bool:
        """Execute full daily backup cycle. Returns True on success.
        Guarded by HFT_BACKUP_ENABLED env var — returns False immediately if not '1'.
        """
        if os.getenv("HFT_BACKUP_ENABLED", "0") != "1":
            logger.info("Backup disabled (HFT_BACKUP_ENABLED != 1), skipping")
            return False
        now = datetime.now(tz=ZoneInfo("Asia/Taipei"))
        backup_name = f"daily_{now.strftime('%Y%m%d')}"
        try:
            self._check_disk_registered()
            self._abort_stale_backups()
            self._execute_backup(backup_name)
            self._verify_backup(backup_name)
            self._cleanup_old_backups()
            self._run_archive_hook(backup_name)
            self._report_success(backup_name)
            return True
        except BackupError as exc:
            self._report_failure(backup_name, exc)
            return False

    def _check_disk_registered(self) -> None:
        """Verify backup_local disk exists: SELECT name FROM system.disks WHERE name='backup_local'.
        Raises BackupError if disk not found (ClickHouse restart needed)."""

    def _abort_stale_backups(self) -> None:
        """Check system.backups for any entry with status='CREATING_BACKUP'.
        If found, log warning and wait/abort before issuing new BACKUP command."""

    def _execute_backup(self, backup_name: str) -> None:
        """BACKUP DATABASE hft TO Disk('backup_local', '{name}/')
        Synchronous — blocks until backup completes."""

    def _verify_backup(self, backup_name: str) -> None:
        """Check system.backups status == 'BACKUP_CREATED' and dir size > 0."""

    def _cleanup_old_backups(self) -> None:
        """Remove backup directories older than retain_days.
        Cleanup is based on directory name parsing (daily_YYYYMMDD format),
        not filesystem mtime, to avoid accidental deletion of manually created entries."""

    def _run_archive_hook(self, backup_name: str) -> None:
        """Override point for cold archival. No-op by default."""
        pass

    def _report_success(self, backup_name: str) -> None:
        """Update Prometheus metrics + send Telegram notification.
        Uses asyncio.run() to bridge sync→async NotificationDispatcher call."""

    def _report_failure(self, backup_name: str, error: Exception) -> None:
        """Update metrics + send failure Telegram notification.
        Uses asyncio.run() to bridge sync→async NotificationDispatcher call."""

    def restore(self, backup_name: str, target_db: str = "hft") -> None:
        """RESTORE DATABASE hft AS {target_db} FROM Disk('backup_local', '{name}/')"""

    def restore_table(self, backup_name: str, table: str, target_db: str = "hft") -> None:
        """RESTORE TABLE hft.{table} AS {target_db}.{table} FROM Disk(...)"""

    def verify_restore(self, backup_name: str) -> dict[str, tuple[int, int]]:
        """Restore to temp DB (AS hft_restore_test), compare row counts, drop temp DB.
        Returns {table_name: (original_count, restored_count)}.
        Raises BackupError if any table has count mismatch > 0 rows."""

    def list_backups(self) -> list[dict]:
        """List available backups with name, date, size."""
```

Note: `BackupManager` is synchronous (designed for cron invocation via shell script).
Async `NotificationDispatcher` calls are bridged via `asyncio.run()` in `_report_success`/`_report_failure`.
Date formatting uses `datetime.now(tz=ZoneInfo("Asia/Taipei"))` — this is cold-path scheduling code,
not hot-path, so `datetime` usage is acceptable per the Alpha Module Float Exception (Rule 11).

Custom exception:

```python
class BackupError(Exception):
    """Raised when backup execution or verification fails."""
```

### 3. Restore Flow

**Disaster recovery** (`scripts/clickhouse_restore.sh <backup_name>`):

```bash
#!/usr/bin/env bash
# Usage: ./scripts/clickhouse_restore.sh daily_20260325
BACKUP_NAME="${1:?Usage: $0 <backup_name>}"
docker exec clickhouse clickhouse-client \
  --query "RESTORE DATABASE hft FROM Disk('backup_local', '${BACKUP_NAME}/')"
```

**Restore verification** (`scripts/clickhouse_restore_verify.sh <backup_name>`):

1. `RESTORE DATABASE hft AS hft_restore_test FROM Disk('backup_local', 'daily_YYYYMMDD/')`
2. Compare row counts per table
3. `DROP DATABASE hft_restore_test`

| Scenario | Command |
|----------|---------|
| Full DB restore | `RESTORE DATABASE hft FROM Disk('backup_local', 'daily_YYYYMMDD/')` |
| Single table restore | `RESTORE TABLE hft.market_data FROM Disk('backup_local', 'daily_YYYYMMDD/')` |
| Verify without touching prod | `BackupManager.verify_restore('daily_YYYYMMDD')` |

### 4. Scheduling

Backup runs daily at **14:30 TWD** (UTC+8), triggered by host crontab or Docker cron:

```cron
30 14 * * * /opt/hft/scripts/clickhouse_backup.sh >> /var/log/hft-backup.log 2>&1
```

**Why 14:30**: TWSE stocks close 13:30, TAIFEX day session closes 13:45. 45 minutes buffer for final data writes. Night session (15:00-05:00) hasn't started yet.

Night session data is captured in the next day's backup.

### 5. Notification Extension

Extend existing `NotificationDispatcher` (16 existing methods → 18):

| Method | Trigger | Critical |
|--------|---------|----------|
| `notify_backup_success(date_str, size_mb, duration_s, retained_count)` | Backup + verify passed | No |
| `notify_backup_failed(date_str, error, last_success_date)` | Backup or verify failed | No (rate-limited, uses distinct header for visibility) |

Template messages:

```
# Success
Backup {date_str} completed
Size: {size_mb} MB | Duration: {duration_s}s
Retained: {retained_count} backups

# Failure
BACKUP FAILED {date_str}
Error: {error}
Last successful backup: {last_success_date}
ACTION REQUIRED: Check ClickHouse backup disk
```

### 6. Prometheus Metrics

4 new Gauge metrics added to `MetricsRegistry`:

| Metric | Type | Description |
|--------|------|-------------|
| `hft_backup_last_success_ts` | Gauge | Unix timestamp of last successful backup |
| `hft_backup_size_bytes` | Gauge | Size of most recent backup in bytes |
| `hft_backup_duration_seconds` | Gauge | Duration of most recent backup |
| `hft_backup_retained_count` | Gauge | Number of backups currently retained |

Alertmanager rule (added to existing Prometheus config):

```yaml
- alert: BackupStale
  expr: hft_backup_last_success_ts < (time() - 172800)  # 2 days
  for: 1h
  labels:
    severity: critical
  annotations:
    summary: "ClickHouse backup is stale (>2 days since last success)"
```

### 7. Cold Archival Hook (Reserved)

`BackupManager._run_archive_hook()` is a no-op in this implementation. Future archival can be added by:

1. Subclassing `BackupManager` and overriding `_run_archive_hook()`
2. Or injecting an `Archiver` callback via constructor

Expected archival interface (not implemented):

```python
class Archiver(Protocol):
    def archive(self, backup_name: str, backup_path: Path) -> None: ...
```

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_BACKUP_ENABLED` | `0` | Enable automated backup |
| `HFT_BACKUP_RETAIN_DAYS` | `30` | Number of daily backups to retain |
| `CH_BACKUP_PATH` | `./backups/clickhouse` | Host path for backup volume mount |

## Testing Strategy

| Test | Type | What it validates |
|------|------|-------------------|
| BackupManager unit tests | Unit | Backup SQL generation, cleanup logic, error handling |
| Notification tests | Unit | Template rendering, dispatcher routing |
| Metrics tests | Unit | Gauge updates on success/failure |
| Restore verification | Integration | End-to-end backup → restore → row count comparison |
| Cron script smoke test | Integration | Script runs without error, produces backup directory |

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Backup disk fills up | Backups stop | `hft_backup_retained_count` metric + alert, cleanup script |
| Backup corrupted silently | False confidence | Post-backup verification (status + size check) |
| Restore untested | Unusable backups | `verify_restore()` method for periodic dry-run |
| Cron not running | Missed backups | `BackupStale` Alertmanager rule (>2 days) |
| backup_local disk not registered | BACKUP SQL fails | `_check_disk_registered()` pre-flight check |
| Previous backup still in progress | New BACKUP collides | `_abort_stale_backups()` checks system.backups for CREATING_BACKUP |
| ClickHouse restart needed after config | First backup fails | Smoke test in deployment: `SELECT name FROM system.disks WHERE name='backup_local'` |

## File Inventory

| Action | File |
|--------|------|
| Create | `config/clickhouse_backup.xml` |
| Create | `src/hft_platform/ops/backup.py` |
| Create | `scripts/clickhouse_backup.sh` |
| Create | `scripts/clickhouse_restore.sh` |
| Create | `scripts/clickhouse_restore_verify.sh` |
| Create | `tests/unit/test_backup_manager.py` |
| Modify | `docker-compose.yml` (add volume mount + config) |
| Modify | `src/hft_platform/notifications/dispatcher.py` (+2 methods) |
| Modify | `src/hft_platform/notifications/templates.py` (+2 functions) |
| Modify | `src/hft_platform/observability/metrics.py` (+4 gauges) |
| Modify | `config/monitoring/alerts/rules.yaml` (+1 rule) |
