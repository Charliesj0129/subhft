"""ClickHouse backup manager with daily scheduling, verification, and notification."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from structlog import get_logger

from hft_platform.infra.ch_client import get_ch_config

if TYPE_CHECKING:
    from hft_platform.notifications.dispatcher import NotificationDispatcher

logger = get_logger("ops.backup")

_TZ_TAIPEI = ZoneInfo("Asia/Taipei")
_BACKUP_NAME_RE = re.compile(r"^daily_(\d{8})$")
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


class BackupError(Exception):
    """Raised when backup execution or verification fails."""


def _get_ch_client(
    host: str,
    port: int,
    user: str,
    password: str,
) -> Any:
    """Return a clickhouse_connect Client (HTTP interface)."""
    from hft_platform.infra.ch_client import get_ch_client

    return get_ch_client(host=host, port=port, username=user, password=password)


class BackupManager:
    """Orchestrates daily ClickHouse backup, verification, cleanup, and notification.

    Designed for synchronous cron invocation. Async NotificationDispatcher
    calls are bridged via asyncio.run().
    """

    __slots__ = (
        "_ch_host",
        "_ch_port",
        "_ch_user",
        "_ch_password",
        "_retain_days",
        "_backup_dir",
        "_notifier",
        "_metrics",
    )

    def __init__(
        self,
        retain_days: int | None = None,
        backup_dir: str = "/backups",
        notifier: NotificationDispatcher | None = None,
        ch_host: str | None = None,
        ch_port: int | None = None,
        ch_user: str | None = None,
        ch_password: str | None = None,
    ) -> None:
        _cfg = get_ch_config()
        self._ch_host = ch_host or _cfg["host"]
        # Uses HTTP port (clickhouse-connect); env default matches platform convention
        self._ch_port = ch_port or int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
        self._ch_user = ch_user or _cfg["username"]
        self._ch_password = ch_password if ch_password is not None else _cfg["password"]
        self._retain_days = retain_days if retain_days is not None else int(os.getenv("HFT_BACKUP_RETAIN_DAYS", "30"))
        self._backup_dir = backup_dir
        self._notifier = notifier
        self._metrics: Any = None

    def _client(self) -> Any:
        assert self._ch_host is not None  # guaranteed by __init__
        assert self._ch_user is not None  # guaranteed by __init__
        return _get_ch_client(self._ch_host, self._ch_port, self._ch_user, self._ch_password)

    def _get_metrics(self) -> Any:
        if self._metrics is None:
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                self._metrics = MetricsRegistry.get()
            except Exception:  # noqa: BLE001
                pass
        return self._metrics

    def run_daily(self) -> bool:
        """Execute full daily backup cycle. Returns True on success."""
        if os.getenv("HFT_BACKUP_ENABLED", "0") != "1":
            logger.info("Backup disabled (HFT_BACKUP_ENABLED != 1), skipping")
            return False

        now = datetime.now(tz=_TZ_TAIPEI)
        backup_name = f"daily_{now.strftime('%Y%m%d')}"
        start = time.monotonic()

        try:
            self._check_disk_registered()
            self._abort_stale_backups()
            self._execute_backup(backup_name)
            self._verify_backup(backup_name)
            self._cleanup_old_backups()
            self._run_archive_hook(backup_name)
            duration_s = time.monotonic() - start
            self._report_success(backup_name, duration_s)
            return True
        except BackupError as exc:
            duration_s = time.monotonic() - start
            self._report_failure(backup_name, exc, duration_s)
            return False

    # ── Pre-flight checks ───────────────────────────────────────

    def _check_disk_registered(self) -> None:
        """Verify backup_local disk is registered in ClickHouse."""
        client = self._client()
        rows = client.query("SELECT name FROM system.disks WHERE name = 'backup_local'").result_rows
        if not rows:
            raise BackupError(
                "backup_local disk not found in system.disks. "
                "Ensure clickhouse_backup.xml is mounted and ClickHouse was restarted."
            )

    def _abort_stale_backups(self) -> None:
        """Check for in-progress backups and wait/abort."""
        client = self._client()
        rows = client.query("SELECT id, name, status FROM system.backups WHERE status = 'CREATING_BACKUP'").result_rows
        if rows:
            logger.warning("Stale backup in progress, aborting", stale_backups=rows)
            raise BackupError(f"Found {len(rows)} in-progress backup(s): {rows}")

    # ── Core operations ─────────────────────────────────────────

    def _execute_backup(self, backup_name: str) -> None:
        """Execute BACKUP DATABASE hft TO Disk('backup_local', ...)."""
        if not _BACKUP_NAME_RE.match(backup_name):
            raise ValueError(f"Invalid backup_name: {backup_name!r}")
        client = self._client()
        sql = f"BACKUP DATABASE hft TO Disk('backup_local', '{backup_name}/')"
        logger.info("Executing backup", backup_name=backup_name, sql=sql)
        client.command(sql)

    def _verify_backup(self, backup_name: str) -> None:
        """Verify backup completed successfully."""
        client = self._client()
        rows = client.query(
            "SELECT status, error FROM system.backups WHERE name = {name:String} ORDER BY start_time DESC LIMIT 1",
            parameters={"name": backup_name},
        ).result_rows
        if not rows:
            raise BackupError(f"No backup entry found in system.backups for '{backup_name}'")
        status, error = rows[0]
        if status != "BACKUP_CREATED":
            raise BackupError(f"Backup '{backup_name}' status is '{status}': {error}")

        backup_path = Path(self._backup_dir) / backup_name
        if not backup_path.exists():
            raise BackupError(f"Backup directory does not exist: {backup_path}")

        size = sum(f.stat().st_size for f in backup_path.rglob("*") if f.is_file())
        if size == 0:
            raise BackupError(f"Backup directory is empty: {backup_path}")
        logger.info("Backup verified", backup_name=backup_name, size_bytes=size)

    def _cleanup_old_backups(self) -> None:
        """Remove backups older than retain_days based on directory name parsing."""
        backup_root = Path(self._backup_dir)
        if not backup_root.exists():
            return

        now = datetime.now(tz=_TZ_TAIPEI)
        removed = 0
        for entry in sorted(backup_root.iterdir()):
            if not entry.is_dir():
                continue
            match = _BACKUP_NAME_RE.match(entry.name)
            if not match:
                continue
            try:
                backup_date = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=_TZ_TAIPEI)
            except ValueError:
                continue
            age_days = (now - backup_date).days
            if age_days > self._retain_days:
                logger.info("Removing old backup", name=entry.name, age_days=age_days)
                shutil.rmtree(entry)
                removed += 1

        if removed:
            logger.info("Cleanup complete", removed=removed)

    def _run_archive_hook(self, backup_name: str) -> None:
        """Override point for cold archival. No-op by default."""

    # ── Reporting ───────────────────────────────────────────────

    def _backup_size_bytes(self, backup_name: str) -> int:
        backup_path = Path(self._backup_dir) / backup_name
        if not backup_path.exists():
            return 0
        return sum(f.stat().st_size for f in backup_path.rglob("*") if f.is_file())

    def _count_retained(self) -> int:
        backup_root = Path(self._backup_dir)
        if not backup_root.exists():
            return 0
        return sum(1 for d in backup_root.iterdir() if d.is_dir() and _BACKUP_NAME_RE.match(d.name))

    def _report_success(self, backup_name: str, duration_s: float) -> None:
        size_bytes = self._backup_size_bytes(backup_name)
        retained = self._count_retained()
        size_mb = size_bytes / (1024 * 1024)
        now_ts = time.time()

        metrics = self._get_metrics()
        if metrics:
            metrics.backup_last_success_ts.set(now_ts)
            metrics.backup_size_bytes.set(size_bytes)
            metrics.backup_duration_seconds.set(duration_s)
            metrics.backup_retained_count.set(retained)

        logger.info(
            "Backup success",
            backup_name=backup_name,
            size_mb=round(size_mb, 1),
            duration_s=round(duration_s, 1),
            retained=retained,
        )

        if self._notifier:
            import asyncio

            asyncio.run(
                self._notifier.notify_backup_success(
                    date_str=backup_name.removeprefix("daily_"),
                    size_mb=round(size_mb, 1),
                    duration_s=round(duration_s, 1),
                    retained_count=retained,
                )
            )

    def _report_failure(self, backup_name: str, error: Exception, duration_s: float) -> None:
        # Find last success date
        last_success = "unknown"
        backup_root = Path(self._backup_dir)
        if backup_root.exists():
            dates = []
            for d in backup_root.iterdir():
                m = _BACKUP_NAME_RE.match(d.name)
                if m and d.name != backup_name:
                    dates.append(m.group(1))
            if dates:
                last_success = max(dates)

        metrics = self._get_metrics()
        if metrics:
            metrics.backup_duration_seconds.set(duration_s)

        logger.warning(
            "Backup failed",
            backup_name=backup_name,
            error=str(error),
            last_success=last_success,
        )

        if self._notifier:
            import asyncio

            asyncio.run(
                self._notifier.notify_backup_failed(
                    date_str=backup_name.removeprefix("daily_"),
                    error=str(error),
                    last_success_date=last_success,
                )
            )

    # ── Restore operations ──────────────────────────────────────

    def restore(self, backup_name: str, target_db: str = "hft") -> None:
        """Restore full database from backup."""
        if not _BACKUP_NAME_RE.match(backup_name):
            raise ValueError(f"Invalid backup_name: {backup_name!r}")
        if not _IDENTIFIER_RE.match(target_db):
            raise ValueError(f"Invalid target_db: {target_db!r}")
        client = self._client()
        if target_db == "hft":
            sql = f"RESTORE DATABASE hft FROM Disk('backup_local', '{backup_name}/')"
        else:
            sql = f"RESTORE DATABASE hft AS {target_db} FROM Disk('backup_local', '{backup_name}/')"
        logger.info("Executing restore", backup_name=backup_name, target_db=target_db)
        client.command(sql)

    def restore_table(self, backup_name: str, table: str, target_db: str = "hft") -> None:
        """Restore a single table from backup."""
        if not _BACKUP_NAME_RE.match(backup_name):
            raise ValueError(f"Invalid backup_name: {backup_name!r}")
        if not _IDENTIFIER_RE.match(table):
            raise ValueError(f"Invalid table: {table!r}")
        if not _IDENTIFIER_RE.match(target_db):
            raise ValueError(f"Invalid target_db: {target_db!r}")
        client = self._client()
        if target_db == "hft":
            sql = f"RESTORE TABLE hft.{table} FROM Disk('backup_local', '{backup_name}/')"
        else:
            sql = f"RESTORE TABLE hft.{table} AS {target_db}.{table} FROM Disk('backup_local', '{backup_name}/')"
        logger.info("Executing table restore", backup_name=backup_name, table=table, target_db=target_db)
        client.command(sql)

    def verify_restore(self, backup_name: str) -> dict[str, tuple[int, int]]:
        """Restore to temp DB, compare row counts, drop temp DB.

        Returns {table_name: (original_count, restored_count)}.
        Raises BackupError if any mismatch found.
        """
        temp_db = "hft_restore_test"
        client = self._client()

        try:
            self.restore(backup_name, target_db=temp_db)

            result = client.query("SELECT name FROM system.tables WHERE database = 'hft'")
            tables = [row[0] for row in result.result_rows]

            if not _IDENTIFIER_RE.match(temp_db):
                raise ValueError(f"Invalid temp_db: {temp_db!r}")

            results: dict[str, tuple[int, int]] = {}
            mismatches: list[str] = []

            for table in tables:
                if not _IDENTIFIER_RE.match(table):
                    logger.warning("Skipping table with invalid identifier", table=table)
                    continue
                orig = client.query(f"SELECT count() FROM hft.{table}").result_rows[0][0]
                restored = client.query(f"SELECT count() FROM {temp_db}.{table}").result_rows[0][0]
                results[table] = (orig, restored)
                if orig != restored:
                    mismatches.append(f"{table}: orig={orig} restored={restored}")

            if mismatches:
                raise BackupError(f"Row count mismatches: {'; '.join(mismatches)}")

            return results
        finally:
            try:
                client.command(f"DROP DATABASE IF EXISTS {temp_db}")
            except Exception:  # noqa: BLE001
                logger.warning("Failed to drop temp DB", temp_db=temp_db)

    def list_backups(self) -> list[dict[str, Any]]:
        """List available backups with name, date, size."""
        backup_root = Path(self._backup_dir)
        if not backup_root.exists():
            return []

        backups: list[dict[str, Any]] = []
        for entry in sorted(backup_root.iterdir()):
            if not entry.is_dir():
                continue
            match = _BACKUP_NAME_RE.match(entry.name)
            if not match:
                continue
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            backups.append(
                {
                    "name": entry.name,
                    "date": match.group(1),
                    "size_bytes": size,
                    "size_mb": round(size / (1024 * 1024), 1),
                }
            )
        return backups
