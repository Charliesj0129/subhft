# ClickHouse Backup & Data Safety Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automated daily ClickHouse backup with 30-day retention, verified restores, and Telegram/Prometheus observability.

**Architecture:** ClickHouse native `BACKUP/RESTORE` to a local Docker volume. Synchronous `BackupManager` orchestrates the full cycle (backup → verify → cleanup → notify). Cron triggers daily at 14:30 TWD. Existing `NotificationDispatcher` extended with 2 methods; `MetricsRegistry` extended with 4 Gauges.

**Tech Stack:** ClickHouse 25.12.3 native backup, `clickhouse_driver` Python client, Prometheus `Gauge`, existing Telegram notification pipeline.

**Spec:** `docs/superpowers/specs/2026-03-25-clickhouse-backup-safety-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `config/clickhouse_backup.xml` | Declare `backup_local` disk in ClickHouse |
| Create | `src/hft_platform/ops/backup.py` | `BackupError` + `BackupManager` class |
| Create | `scripts/clickhouse_backup.sh` | Cron entry point wrapper |
| Create | `scripts/clickhouse_restore.sh` | Manual disaster recovery |
| Create | `scripts/clickhouse_restore_verify.sh` | Restore dry-run verification |
| Create | `tests/unit/test_backup_manager.py` | Unit tests for BackupManager |
| Create | `tests/unit/test_backup_notifications.py` | Unit tests for notification + template extensions |
| Create | `tests/unit/test_backup_metrics.py` | Unit tests for backup Prometheus metrics |
| Modify | `docker-compose.yml:127-138` | Add backup volume mount + config |
| Modify | `src/hft_platform/notifications/templates.py:389` | Add `render_backup_success`, `render_backup_failed` |
| Modify | `src/hft_platform/notifications/dispatcher.py:400` | Add `notify_backup_success`, `notify_backup_failed` |
| Modify | `src/hft_platform/observability/metrics.py:749` | Add 4 backup Gauge metrics |
| Modify | `config/monitoring/alerts/rules.yaml:487` | Add `BackupStale` alert rule |

---

### Task 1: ClickHouse Backup Disk Config + Docker Volume

**Files:**
- Create: `config/clickhouse_backup.xml`
- Modify: `docker-compose.yml:127-138`

- [ ] **Step 1: Create ClickHouse backup disk config**

```xml
<!-- config/clickhouse_backup.xml -->
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

- [ ] **Step 2: Add volume mounts to docker-compose.yml**

In the `clickhouse` service `volumes:` section (after line 133, the memory.xml mount), add:

```yaml
      # Backup disk config + backup storage volume
      - ./config/clickhouse_backup.xml:/etc/clickhouse-server/config.d/backup.xml:ro
      - ${CH_BACKUP_PATH:-./backups/clickhouse}:/backups
```

- [ ] **Step 3: Verify — restart ClickHouse and check disk registration**

```bash
# Restart ClickHouse to pick up new disk config
docker compose restart clickhouse

# Wait for healthy, then verify disk exists
docker exec clickhouse clickhouse-client \
  --query "SELECT name, path, type FROM system.disks WHERE name = 'backup_local'"
```

Expected output: one row with `backup_local`, `/backups/`, `local`.

- [ ] **Step 4: Commit**

```bash
git add config/clickhouse_backup.xml docker-compose.yml
git commit -m "feat(ops): add ClickHouse backup disk config and Docker volume mount"
```

---

### Task 2: Notification Templates (render_backup_success, render_backup_failed)

**Files:**
- Modify: `src/hft_platform/notifications/templates.py`
- Create: `tests/unit/test_backup_notifications.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_backup_notifications.py`:

```python
"""Tests for backup notification templates and dispatcher methods."""

from __future__ import annotations


def test_render_backup_success_contains_key_fields():
    from hft_platform.notifications.templates import render_backup_success

    msg = render_backup_success(
        date_str="2026-03-25",
        size_mb=1234.5,
        duration_s=42.3,
        retained_count=15,
    )
    assert "2026-03-25" in msg
    assert "1,234.5" in msg or "1234.5" in msg
    assert "42.3" in msg
    assert "15" in msg


def test_render_backup_failed_contains_error_and_last_success():
    from hft_platform.notifications.templates import render_backup_failed

    msg = render_backup_failed(
        date_str="2026-03-25",
        error="Disk full",
        last_success_date="2026-03-24",
    )
    assert "2026-03-25" in msg
    assert "Disk full" in msg
    assert "2026-03-24" in msg
    assert "FAIL" in msg.upper() or "失敗" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_backup_notifications.py -v
```

Expected: FAIL with `ImportError` or `cannot import name 'render_backup_success'`

- [ ] **Step 3: Implement the templates**

Add at the end of `src/hft_platform/notifications/templates.py` (after `render_shadow_daily_report`):

```python


def render_backup_success(
    *,
    date_str: str,
    size_mb: float,
    duration_s: float,
    retained_count: int,
) -> str:
    """Daily ClickHouse backup completed successfully.

    Args:
        date_str: Date label, e.g. "2026-03-25".
        size_mb: Backup size in megabytes.
        duration_s: Backup duration in seconds.
        retained_count: Number of backups currently retained on disk.

    Returns:
        Formatted backup success notification string.
    """
    return (
        f"🟢 Backup {date_str} 完成\n"
        f"大小: {size_mb:,.1f} MB | 耗時: {duration_s:.1f}s\n"
        f"保留: {retained_count} 份備份"
    )


def render_backup_failed(
    *,
    date_str: str,
    error: str,
    last_success_date: str,
) -> str:
    """Daily ClickHouse backup failed.

    Args:
        date_str: Date label, e.g. "2026-03-25".
        error: Error message describing the failure.
        last_success_date: Date of the last successful backup, e.g. "2026-03-24".

    Returns:
        Formatted backup failure notification string.
    """
    return (
        f"🔴 BACKUP 失敗 {date_str}\n"
        f"錯誤: {error}\n"
        f"最後成功備份: {last_success_date}\n"
        f"請立即檢查備份磁碟"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_backup_notifications.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/notifications/templates.py tests/unit/test_backup_notifications.py
git commit -m "feat(notifications): add backup success/failure message templates"
```

---

### Task 3: Notification Dispatcher Extension

**Files:**
- Modify: `src/hft_platform/notifications/dispatcher.py`
- Modify: `tests/unit/test_backup_notifications.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_backup_notifications.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock


def test_notify_backup_success_sends_non_critical():
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    sender = MagicMock()
    sender.send = AsyncMock()
    dispatcher = NotificationDispatcher(sender=sender)

    asyncio.run(
        dispatcher.notify_backup_success(
            date_str="2026-03-25",
            size_mb=1234.5,
            duration_s=42.3,
            retained_count=15,
        )
    )
    sender.send.assert_called_once()
    assert sender.send.call_args.kwargs.get("critical") is False


def test_notify_backup_failed_sends_non_critical():
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    sender = MagicMock()
    sender.send = AsyncMock()
    dispatcher = NotificationDispatcher(sender=sender)

    asyncio.run(
        dispatcher.notify_backup_failed(
            date_str="2026-03-25",
            error="Disk full",
            last_success_date="2026-03-24",
        )
    )
    sender.send.assert_called_once()
    assert sender.send.call_args.kwargs.get("critical") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_backup_notifications.py::test_notify_backup_success_sends_non_critical -v
```

Expected: FAIL with `AttributeError: 'NotificationDispatcher' object has no attribute 'notify_backup_success'`

- [ ] **Step 3: Add dispatcher methods**

Add at the end of `NotificationDispatcher` class in `src/hft_platform/notifications/dispatcher.py` (after `notify_shadow_daily_report`, before end of class):

```python
    async def notify_backup_success(
        self,
        *,
        date_str: str,
        size_mb: float,
        duration_s: float,
        retained_count: int,
    ) -> None:
        """Notify operator of a successful ClickHouse backup.

        Args:
            date_str: Date label, e.g. "2026-03-25".
            size_mb: Backup size in megabytes.
            duration_s: Backup duration in seconds.
            retained_count: Number of backups currently retained.
        """
        msg = templates.render_backup_success(
            date_str=date_str,
            size_mb=size_mb,
            duration_s=duration_s,
            retained_count=retained_count,
        )
        logger.info(
            "dispatcher.notify_backup_success",
            date_str=date_str,
            size_mb=size_mb,
        )
        await self._sender.send(msg, critical=False)

    async def notify_backup_failed(
        self,
        *,
        date_str: str,
        error: str,
        last_success_date: str,
    ) -> None:
        """Notify operator of a failed ClickHouse backup.

        Args:
            date_str: Date label, e.g. "2026-03-25".
            error: Error message describing the failure.
            last_success_date: Date of the last successful backup.
        """
        msg = templates.render_backup_failed(
            date_str=date_str,
            error=error,
            last_success_date=last_success_date,
        )
        logger.warning(
            "dispatcher.notify_backup_failed",
            date_str=date_str,
            error=error,
        )
        await self._sender.send(msg, critical=False)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_backup_notifications.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/notifications/dispatcher.py tests/unit/test_backup_notifications.py
git commit -m "feat(notifications): add backup success/failure dispatcher methods"
```

---

### Task 4: Prometheus Metrics Extension

**Files:**
- Modify: `src/hft_platform/observability/metrics.py:749`
- Create: `tests/unit/test_backup_metrics.py`

- [ ] **Step 1: Write failing test for metrics attributes**

Create `tests/unit/test_backup_metrics.py`:

```python
"""Tests for backup-related Prometheus metrics."""

from __future__ import annotations


def test_metrics_registry_has_backup_gauges():
    from hft_platform.observability.metrics import MetricsRegistry

    m = MetricsRegistry.get()
    assert hasattr(m, "backup_last_success_ts")
    assert hasattr(m, "backup_size_bytes")
    assert hasattr(m, "backup_duration_seconds")
    assert hasattr(m, "backup_retained_count")


def test_backup_gauges_are_settable():
    from hft_platform.observability.metrics import MetricsRegistry

    m = MetricsRegistry.get()
    m.backup_last_success_ts.set(1711324800.0)
    m.backup_size_bytes.set(1024)
    m.backup_duration_seconds.set(5.5)
    m.backup_retained_count.set(15)
    # If no exception, gauges work correctly
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_backup_metrics.py -v
```

Expected: FAIL with `AttributeError: 'MetricsRegistry' object has no attribute 'backup_last_success_ts'`

- [ ] **Step 3: Add backup metrics to MetricsRegistry**

In `src/hft_platform/observability/metrics.py`, inside `MetricsRegistry.__init__()`, add before the `try: import psutil` block (around line 749):

```python
        # ── Backup Metrics ──────────────────────────────────────────
        self.backup_last_success_ts = Gauge(
            "hft_backup_last_success_ts",
            "Unix timestamp of last successful ClickHouse backup",
        )
        self.backup_size_bytes = Gauge(
            "hft_backup_size_bytes",
            "Size of most recent ClickHouse backup in bytes",
        )
        self.backup_duration_seconds = Gauge(
            "hft_backup_duration_seconds",
            "Duration of most recent ClickHouse backup in seconds",
        )
        self.backup_retained_count = Gauge(
            "hft_backup_retained_count",
            "Number of ClickHouse backups currently retained on disk",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_backup_metrics.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/observability/metrics.py tests/unit/test_backup_metrics.py
git commit -m "feat(metrics): add 4 ClickHouse backup Gauge metrics"
```

---

### Task 5: Alertmanager Rule for Stale Backup

**Files:**
- Modify: `config/monitoring/alerts/rules.yaml`

- [ ] **Step 1: Add BackupStale alert rule**

Append to the end of `config/monitoring/alerts/rules.yaml` (after the last rule, around line 487):

```yaml
  # ── Backup Health ─────────────────────────────────────────────
  - alert: BackupStale
    expr: hft_backup_last_success_ts < (time() - 172800)
    for: 1h
    labels:
        severity: critical
    annotations:
        summary: ClickHouse backup is stale (>2 days since last success)
        description: "Last successful backup was {{ $value | humanizeTimestamp }}. Check cron and backup disk."
```

- [ ] **Step 2: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('config/monitoring/alerts/rules.yaml'))" && echo "YAML valid"
```

Expected: `YAML valid`

- [ ] **Step 3: Commit**

```bash
git add config/monitoring/alerts/rules.yaml
git commit -m "feat(alerts): add BackupStale alert rule (>2 days without success)"
```

---

### Task 6: BackupManager Core — BackupError + Constructor + run_daily skeleton

**Files:**
- Create: `src/hft_platform/ops/backup.py`
- Create: `tests/unit/test_backup_manager.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_backup_manager.py`:

```python
"""Tests for BackupManager."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


def test_backup_error_is_exception():
    from hft_platform.ops.backup import BackupError

    assert issubclass(BackupError, Exception)
    err = BackupError("disk full")
    assert str(err) == "disk full"


def test_constructor_defaults_from_env():
    with patch.dict(os.environ, {
        "HFT_CLICKHOUSE_HOST": "ch-host",
        "HFT_CLICKHOUSE_PORT": "9999",
        "HFT_CLICKHOUSE_USER": "admin",
        "HFT_CLICKHOUSE_PASSWORD": "secret",
    }):
        from hft_platform.ops.backup import BackupManager

        mgr = BackupManager()
        assert mgr._ch_host == "ch-host"
        assert mgr._ch_port == 9999
        assert mgr._ch_user == "admin"
        assert mgr._ch_password == "secret"
        assert mgr._retain_days == 30
        assert mgr._backup_dir == "/backups"
        assert mgr._notifier is None


def test_constructor_explicit_params_override_env():
    from hft_platform.ops.backup import BackupManager

    mgr = BackupManager(
        ch_host="explicit",
        ch_port=1234,
        retain_days=7,
        backup_dir="/tmp/bk",
    )
    assert mgr._ch_host == "explicit"
    assert mgr._ch_port == 1234
    assert mgr._retain_days == 7


def test_run_daily_returns_false_when_disabled():
    from hft_platform.ops.backup import BackupManager

    with patch.dict(os.environ, {"HFT_BACKUP_ENABLED": "0"}):
        mgr = BackupManager()
        result = mgr.run_daily()
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/unit/test_backup_manager.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.ops.backup'`

- [ ] **Step 3: Implement BackupManager skeleton**

Create `src/hft_platform/ops/backup.py`:

```python
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

if TYPE_CHECKING:
    from hft_platform.notifications.dispatcher import NotificationDispatcher

logger = get_logger("ops.backup")

_TZ_TAIPEI = ZoneInfo("Asia/Taipei")
_BACKUP_NAME_RE = re.compile(r"^daily_(\d{8})$")


class BackupError(Exception):
    """Raised when backup execution or verification fails."""


def _get_ch_client(
    host: str, port: int, user: str, password: str,
) -> Any:
    """Return a clickhouse_driver Client."""
    try:
        from clickhouse_driver import Client
    except ImportError as exc:
        raise RuntimeError("clickhouse_driver is not installed") from exc
    return Client(host=host, port=port, user=user, password=password)


class BackupManager:
    """Orchestrates daily ClickHouse backup, verification, cleanup, and notification.

    Designed for synchronous cron invocation. Async NotificationDispatcher
    calls are bridged via asyncio.run().
    """

    __slots__ = (
        "_ch_host", "_ch_port", "_ch_user", "_ch_password",
        "_retain_days", "_backup_dir", "_notifier", "_metrics",
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
        self._ch_host = ch_host or os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
        self._ch_port = ch_port or int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
        self._ch_user = ch_user or os.getenv("HFT_CLICKHOUSE_USER", "default")
        self._ch_password = ch_password if ch_password is not None else os.getenv("HFT_CLICKHOUSE_PASSWORD", "")
        self._retain_days = retain_days if retain_days is not None else int(os.getenv("HFT_BACKUP_RETAIN_DAYS", "30"))
        self._backup_dir = backup_dir
        self._notifier = notifier
        self._metrics: Any = None

    def _client(self) -> Any:
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
        rows = client.execute("SELECT name FROM system.disks WHERE name = 'backup_local'")
        if not rows:
            raise BackupError(
                "backup_local disk not found in system.disks. "
                "Ensure clickhouse_backup.xml is mounted and ClickHouse was restarted."
            )

    def _abort_stale_backups(self) -> None:
        """Check for in-progress backups and wait/abort."""
        client = self._client()
        rows = client.execute(
            "SELECT id, name, status FROM system.backups WHERE status = 'CREATING_BACKUP'"
        )
        if rows:
            logger.warning("Stale backup in progress, aborting", stale_backups=rows)
            raise BackupError(f"Found {len(rows)} in-progress backup(s): {rows}")

    # ── Core operations ─────────────────────────────────────────

    def _execute_backup(self, backup_name: str) -> None:
        """Execute BACKUP DATABASE hft TO Disk('backup_local', ...)."""
        client = self._client()
        sql = f"BACKUP DATABASE hft TO Disk('backup_local', '{backup_name}/')"
        logger.info("Executing backup", backup_name=backup_name, sql=sql)
        client.execute(sql)

    def _verify_backup(self, backup_name: str) -> None:
        """Verify backup completed successfully."""
        client = self._client()
        rows = client.execute(
            "SELECT status, error FROM system.backups WHERE name = %(name)s ORDER BY start_time DESC LIMIT 1",
            {"name": backup_name},
        )
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
        client = self._client()
        if target_db == "hft":
            sql = f"RESTORE DATABASE hft FROM Disk('backup_local', '{backup_name}/')"
        else:
            sql = f"RESTORE DATABASE hft AS {target_db} FROM Disk('backup_local', '{backup_name}/')"
        logger.info("Executing restore", backup_name=backup_name, target_db=target_db)
        client.execute(sql)

    def restore_table(self, backup_name: str, table: str, target_db: str = "hft") -> None:
        """Restore a single table from backup."""
        client = self._client()
        if target_db == "hft":
            sql = f"RESTORE TABLE hft.{table} FROM Disk('backup_local', '{backup_name}/')"
        else:
            sql = f"RESTORE TABLE hft.{table} AS {target_db}.{table} FROM Disk('backup_local', '{backup_name}/')"
        logger.info("Executing table restore", backup_name=backup_name, table=table, target_db=target_db)
        client.execute(sql)

    def verify_restore(self, backup_name: str) -> dict[str, tuple[int, int]]:
        """Restore to temp DB, compare row counts, drop temp DB.

        Returns {table_name: (original_count, restored_count)}.
        Raises BackupError if any mismatch found.
        """
        temp_db = "hft_restore_test"
        client = self._client()

        try:
            self.restore(backup_name, target_db=temp_db)

            # Get table list from original DB
            tables = [
                row[0] for row in client.execute(
                    "SELECT name FROM system.tables WHERE database = 'hft'"
                )
            ]

            results: dict[str, tuple[int, int]] = {}
            mismatches: list[str] = []

            for table in tables:
                orig = client.execute(f"SELECT count() FROM hft.{table}")[0][0]
                restored = client.execute(f"SELECT count() FROM {temp_db}.{table}")[0][0]
                results[table] = (orig, restored)
                if orig != restored:
                    mismatches.append(f"{table}: orig={orig} restored={restored}")

            if mismatches:
                raise BackupError(f"Row count mismatches: {'; '.join(mismatches)}")

            return results
        finally:
            try:
                client.execute(f"DROP DATABASE IF EXISTS {temp_db}")
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
            backups.append({
                "name": entry.name,
                "date": match.group(1),
                "size_bytes": size,
                "size_mb": round(size / (1024 * 1024), 1),
            })
        return backups
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/unit/test_backup_manager.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/ops/backup.py tests/unit/test_backup_manager.py
git commit -m "feat(ops): add BackupManager with backup/verify/cleanup/restore/notify"
```

---

### Task 7: BackupManager Unit Tests — Cleanup + Verify + List

**Files:**
- Modify: `tests/unit/test_backup_manager.py`

- [ ] **Step 1: Write additional unit tests**

Append to `tests/unit/test_backup_manager.py`:

```python
from pathlib import Path


def test_cleanup_removes_old_backups(tmp_path):
    from hft_platform.ops.backup import BackupManager

    # Create fake backup dirs: 3 old + 1 recent
    for day_offset, name in [(40, "daily_20260213"), (35, "daily_20260218"), (31, "daily_20260222"), (1, "daily_20260324")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "data.bin").write_bytes(b"x")

    mgr = BackupManager(retain_days=30, backup_dir=str(tmp_path))
    mgr._cleanup_old_backups()

    remaining = sorted(d.name for d in tmp_path.iterdir() if d.is_dir())
    assert "daily_20260324" in remaining
    # All 3 old ones should be removed (age > 30)
    assert "daily_20260213" not in remaining
    assert "daily_20260218" not in remaining
    assert "daily_20260222" not in remaining  # borderline: 31 > 30


def test_cleanup_ignores_non_matching_dirs(tmp_path):
    from hft_platform.ops.backup import BackupManager

    (tmp_path / "manual_backup").mkdir()
    (tmp_path / "daily_20260324").mkdir()

    mgr = BackupManager(retain_days=30, backup_dir=str(tmp_path))
    mgr._cleanup_old_backups()

    assert (tmp_path / "manual_backup").exists(), "Non-matching dir should not be removed"


def test_list_backups_returns_sorted(tmp_path):
    from hft_platform.ops.backup import BackupManager

    for name in ["daily_20260325", "daily_20260323", "daily_20260324"]:
        d = tmp_path / name
        d.mkdir()
        (d / "data.bin").write_bytes(b"x" * 100)

    mgr = BackupManager(backup_dir=str(tmp_path))
    result = mgr.list_backups()

    assert len(result) == 3
    assert result[0]["name"] == "daily_20260323"
    assert result[2]["name"] == "daily_20260325"
    assert all(r["size_bytes"] == 100 for r in result)


def test_list_backups_empty_dir(tmp_path):
    from hft_platform.ops.backup import BackupManager

    mgr = BackupManager(backup_dir=str(tmp_path))
    assert mgr.list_backups() == []


def test_backup_size_bytes(tmp_path):
    from hft_platform.ops.backup import BackupManager

    d = tmp_path / "daily_20260325"
    d.mkdir()
    (d / "a.bin").write_bytes(b"x" * 500)
    (d / "b.bin").write_bytes(b"y" * 300)

    mgr = BackupManager(backup_dir=str(tmp_path))
    assert mgr._backup_size_bytes("daily_20260325") == 800


def test_count_retained(tmp_path):
    from hft_platform.ops.backup import BackupManager

    (tmp_path / "daily_20260323").mkdir()
    (tmp_path / "daily_20260324").mkdir()
    (tmp_path / "not_a_backup").mkdir()

    mgr = BackupManager(backup_dir=str(tmp_path))
    assert mgr._count_retained() == 2
```

- [ ] **Step 2: Run all tests**

```bash
uv run pytest tests/unit/test_backup_manager.py -v
```

Expected: 10 passed

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_backup_manager.py
git commit -m "test(ops): add cleanup, list, and size unit tests for BackupManager"
```

---

### Task 8: Shell Scripts (backup, restore, restore-verify)

**Files:**
- Create: `scripts/clickhouse_backup.sh`
- Create: `scripts/clickhouse_restore.sh`
- Create: `scripts/clickhouse_restore_verify.sh`

- [ ] **Step 1: Create the backup cron entry script**

Create `scripts/clickhouse_backup.sh`:

```bash
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
```

- [ ] **Step 2: Create the restore script**

Create `scripts/clickhouse_restore.sh`:

```bash
#!/usr/bin/env bash
# Disaster recovery: restore ClickHouse database from backup.
# Usage: ./scripts/clickhouse_restore.sh <backup_name>
# Example: ./scripts/clickhouse_restore.sh daily_20260325
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
```

- [ ] **Step 3: Create the restore verification script**

Create `scripts/clickhouse_restore_verify.sh`:

```bash
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
```

- [ ] **Step 4: Make scripts executable**

```bash
chmod +x scripts/clickhouse_backup.sh scripts/clickhouse_restore.sh scripts/clickhouse_restore_verify.sh
```

- [ ] **Step 5: Ensure host backup directory exists**

```bash
mkdir -p ./backups/clickhouse
```

- [ ] **Step 6: Install cron entry (production host only)**

```bash
# Add daily backup cron at 14:30 TWD (skip in CI/dev)
(crontab -l 2>/dev/null; echo "30 14 * * * cd $(pwd) && ./scripts/clickhouse_backup.sh >> /var/log/hft-backup.log 2>&1") | crontab -

# Verify
crontab -l | grep clickhouse_backup
```

Expected: cron line with `30 14 * * *` and `clickhouse_backup.sh`

Note: This step is for the production host only. Skip in CI/dev environments.

- [ ] **Step 7: Commit**

```bash
git add scripts/clickhouse_backup.sh scripts/clickhouse_restore.sh scripts/clickhouse_restore_verify.sh
git commit -m "feat(ops): add backup/restore/verify shell scripts for ClickHouse"
```

---

### Task 9: Lint + CI Verification

**Files:** None (verification only)

- [ ] **Step 1: Run ruff lint on all changed files**

```bash
uv run ruff check src/hft_platform/ops/backup.py src/hft_platform/notifications/templates.py src/hft_platform/notifications/dispatcher.py src/hft_platform/observability/metrics.py
```

Expected: no errors

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/unit/test_backup_manager.py tests/unit/test_backup_notifications.py -v
```

Expected: all tests pass

- [ ] **Step 3: Run make lint + typecheck**

```bash
make lint && make typecheck
```

Expected: both pass

- [ ] **Step 4: Commit any lint fixes if needed**

```bash
git add -u
git commit -m "fix: lint/typecheck fixes for backup feature"
```

---

### Task 10: Final Integration — Documentation Update

**Files:**
- Modify: `docs/superpowers/specs/2026-03-25-clickhouse-backup-safety-design.md`

- [ ] **Step 1: Update spec status from Draft to Implemented**

Change line 4 of the spec:

```markdown
**Status**: Implemented
```

- [ ] **Step 2: Update .env.example with new env vars**

Add to `.env.example`:

```bash
# ── ClickHouse Backup ──────────────────────────────────────
# HFT_BACKUP_ENABLED=1        # Enable automated daily backup (default: 0)
# HFT_BACKUP_RETAIN_DAYS=30   # Number of daily backups to retain (default: 30)
# CH_BACKUP_PATH=./backups/clickhouse  # Host path for backup volume (default: ./backups/clickhouse)
```

- [ ] **Step 3: Update CLAUDE.md env var table**

Add to the `Critical Environment Variables` table in `CLAUDE.md`:

```markdown
| `HFT_BACKUP_ENABLED`        | `0`                    | `1` = enable automated daily ClickHouse backup |
| `HFT_BACKUP_RETAIN_DAYS`    | `30`                   | Number of daily backups to retain               |
| `CH_BACKUP_PATH`            | `./backups/clickhouse`  | Host path for ClickHouse backup volume mount    |
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-03-25-clickhouse-backup-safety-design.md .env.example CLAUDE.md
git commit -m "docs: mark backup spec as implemented, update env var docs"
```
