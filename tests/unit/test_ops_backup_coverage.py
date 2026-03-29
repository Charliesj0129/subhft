"""Coverage tests for hft_platform.ops.backup.

Targets:
- BackupManager.__init__ env var resolution
- run_daily: disabled, success, failure paths
- _check_disk_registered: found/not-found
- _abort_stale_backups: clean/stale
- _verify_backup: no rows, bad status, missing path, empty dir, success
- _cleanup_old_backups: age-based removal
- _report_success / _report_failure: with/without notifier and metrics
- restore / restore_table: invalid name, valid name (both target_db paths)
- verify_restore: success, mismatch, temp-DB cleanup
- list_backups: empty dir, mixed entries
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.ops.backup import BackupError, BackupManager, _BACKUP_NAME_RE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(backup_dir: str = "/tmp/test_backup") -> BackupManager:
    return BackupManager(
        retain_days=7,
        backup_dir=backup_dir,
        ch_host="localhost",
        ch_port=9000,
        ch_user="default",
        ch_password="",
    )


def _mock_client() -> MagicMock:
    """Return a mock ClickHouse client."""
    return MagicMock()


@contextmanager
def _patch_client(mgr: BackupManager, client: MagicMock):
    """Patch BackupManager._client at class level for __slots__ compatibility."""
    with patch.object(BackupManager, "_client", return_value=client):
        yield


# ---------------------------------------------------------------------------
# __init__ env var resolution
# ---------------------------------------------------------------------------


def test_init_picks_up_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_CLICKHOUSE_HOST", "ch-host-from-env")
    monkeypatch.setenv("HFT_CLICKHOUSE_PORT", "19000")
    monkeypatch.setenv("HFT_CLICKHOUSE_USER", "admin")
    monkeypatch.setenv("HFT_CLICKHOUSE_PASSWORD", "secret")
    monkeypatch.setenv("HFT_BACKUP_RETAIN_DAYS", "14")

    mgr = BackupManager(backup_dir="/backups")

    assert mgr._ch_host == "ch-host-from-env"
    assert mgr._ch_port == 19000
    assert mgr._ch_user == "admin"
    assert mgr._ch_password == "secret"
    assert mgr._retain_days == 14


def test_init_explicit_args_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_CLICKHOUSE_HOST", "env-host")
    mgr = BackupManager(
        ch_host="explicit-host",
        ch_port=9999,
        ch_user="myuser",
        ch_password="mypass",
        retain_days=30,
        backup_dir="/data",
    )
    assert mgr._ch_host == "explicit-host"
    assert mgr._ch_port == 9999
    assert mgr._ch_user == "myuser"
    assert mgr._ch_password == "mypass"
    assert mgr._retain_days == 30


# ---------------------------------------------------------------------------
# run_daily: disabled
# ---------------------------------------------------------------------------


def test_run_daily_disabled_when_env_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_BACKUP_ENABLED", "0")
    mgr = _make_manager()
    result = mgr.run_daily()
    assert result is False


def test_run_daily_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HFT_BACKUP_ENABLED", raising=False)
    mgr = _make_manager()
    result = mgr.run_daily()
    assert result is False


# ---------------------------------------------------------------------------
# run_daily: success path
# ---------------------------------------------------------------------------


def test_run_daily_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HFT_BACKUP_ENABLED", "1")
    mgr = _make_manager(backup_dir=str(tmp_path))

    monkeypatch.setattr(BackupManager, "_check_disk_registered", lambda self: None)
    monkeypatch.setattr(BackupManager, "_abort_stale_backups", lambda self: None)
    monkeypatch.setattr(BackupManager, "_execute_backup", lambda self, name: None)
    monkeypatch.setattr(BackupManager, "_verify_backup", lambda self, name: None)
    monkeypatch.setattr(BackupManager, "_cleanup_old_backups", lambda self: None)
    monkeypatch.setattr(BackupManager, "_run_archive_hook", lambda self, name: None)
    monkeypatch.setattr(BackupManager, "_report_success", lambda self, name, dur: None)

    result = mgr.run_daily()
    assert result is True


# ---------------------------------------------------------------------------
# run_daily: failure path
# ---------------------------------------------------------------------------


def test_run_daily_failure_on_check_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HFT_BACKUP_ENABLED", "1")
    mgr = _make_manager(backup_dir=str(tmp_path))

    monkeypatch.setattr(
        BackupManager,
        "_check_disk_registered",
        lambda self: (_ for _ in ()).throw(BackupError("no disk")),
    )
    reported: list[tuple] = []
    monkeypatch.setattr(
        BackupManager,
        "_report_failure",
        lambda self, name, err, dur: reported.append((name, err, dur)),
    )

    result = mgr.run_daily()
    assert result is False
    assert len(reported) == 1
    assert "no disk" in str(reported[0][1])


# ---------------------------------------------------------------------------
# _check_disk_registered
# ---------------------------------------------------------------------------


def test_check_disk_registered_raises_when_missing() -> None:
    mgr = _make_manager()
    client = _mock_client()
    client.execute.return_value = []  # no rows → disk not found

    with _patch_client(mgr, client):
        with pytest.raises(BackupError, match="backup_local disk not found"):
            mgr._check_disk_registered()


def test_check_disk_registered_passes_when_found() -> None:
    mgr = _make_manager()
    client = _mock_client()
    client.execute.return_value = [("backup_local",)]

    with _patch_client(mgr, client):
        mgr._check_disk_registered()  # should not raise


# ---------------------------------------------------------------------------
# _abort_stale_backups
# ---------------------------------------------------------------------------


def test_abort_stale_backups_raises_when_in_progress() -> None:
    mgr = _make_manager()
    client = _mock_client()
    client.execute.return_value = [("abc123", "daily_20260101", "CREATING_BACKUP")]

    with _patch_client(mgr, client):
        with pytest.raises(BackupError, match="in-progress backup"):
            mgr._abort_stale_backups()


def test_abort_stale_backups_passes_when_clean() -> None:
    mgr = _make_manager()
    client = _mock_client()
    client.execute.return_value = []

    with _patch_client(mgr, client):
        mgr._abort_stale_backups()  # should not raise


# ---------------------------------------------------------------------------
# _verify_backup
# ---------------------------------------------------------------------------


def test_verify_backup_raises_when_no_rows() -> None:
    mgr = _make_manager()
    client = _mock_client()
    client.execute.return_value = []

    with _patch_client(mgr, client):
        with pytest.raises(BackupError, match="No backup entry"):
            mgr._verify_backup("daily_20260329")


def test_verify_backup_raises_when_status_not_created() -> None:
    mgr = _make_manager()
    client = _mock_client()
    client.execute.return_value = [("BACKUP_FAILED", "disk full")]

    with _patch_client(mgr, client):
        with pytest.raises(BackupError, match="BACKUP_FAILED"):
            mgr._verify_backup("daily_20260329")


def test_verify_backup_raises_when_path_missing(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    client = _mock_client()
    client.execute.return_value = [("BACKUP_CREATED", "")]

    with _patch_client(mgr, client):
        with pytest.raises(BackupError, match="does not exist"):
            mgr._verify_backup("daily_20260329")


def test_verify_backup_raises_when_empty(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    client = _mock_client()
    client.execute.return_value = [("BACKUP_CREATED", "")]

    # Create the directory but leave it empty
    (tmp_path / "daily_20260329").mkdir()

    with _patch_client(mgr, client):
        with pytest.raises(BackupError, match="empty"):
            mgr._verify_backup("daily_20260329")


def test_verify_backup_success(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    client = _mock_client()
    client.execute.return_value = [("BACKUP_CREATED", "")]

    # Create dir with a real file
    backup_dir = tmp_path / "daily_20260329"
    backup_dir.mkdir()
    (backup_dir / "metadata.json").write_bytes(b"x" * 1024)

    with _patch_client(mgr, client):
        mgr._verify_backup("daily_20260329")  # should not raise


# ---------------------------------------------------------------------------
# _cleanup_old_backups
# ---------------------------------------------------------------------------


def test_cleanup_old_backups_removes_stale(tmp_path: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _TZ_TAIPEI = ZoneInfo("Asia/Taipei")
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    # Create old backup (20 days old relative to our fixed "now")
    old_dir = tmp_path / "daily_20260309"
    old_dir.mkdir()

    # Create recent backup (1 day old)
    recent_dir = tmp_path / "daily_20260328"
    recent_dir.mkdir()

    # Create a non-backup dir that should be ignored
    other_dir = tmp_path / "other_dir"
    other_dir.mkdir()

    fixed_now = datetime(2026, 3, 29, tzinfo=_TZ_TAIPEI)
    with patch("hft_platform.ops.backup.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.strptime.side_effect = datetime.strptime  # preserve strptime
        mgr._cleanup_old_backups()

    assert not old_dir.exists(), "Old backup should be removed"
    assert recent_dir.exists(), "Recent backup should be kept"
    assert other_dir.exists(), "Non-backup dir should be untouched"


def test_cleanup_old_backups_no_dir_is_noop(tmp_path: Path) -> None:
    """Non-existent backup root → _cleanup does nothing."""
    mgr = BackupManager(backup_dir=str(tmp_path / "nonexistent"), retain_days=7, ch_host="localhost")
    mgr._cleanup_old_backups()  # should not raise


# ---------------------------------------------------------------------------
# _report_success / _report_failure
# ---------------------------------------------------------------------------


def test_report_success_no_notifier_no_crash(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    # Create backup dir with a file for size calculation
    backup_dir = tmp_path / "daily_20260329"
    backup_dir.mkdir()
    (backup_dir / "data.bin").write_bytes(b"x" * 512)

    mgr._report_success("daily_20260329", duration_s=1.5)  # should not raise


def test_report_success_with_metrics(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    mock_metrics = MagicMock()
    mgr._metrics = mock_metrics

    backup_dir = tmp_path / "daily_20260329"
    backup_dir.mkdir()
    (backup_dir / "data.bin").write_bytes(b"x" * 1024)

    mgr._report_success("daily_20260329", duration_s=2.0)

    mock_metrics.backup_last_success_ts.set.assert_called_once()
    mock_metrics.backup_size_bytes.set.assert_called_once()
    mock_metrics.backup_duration_seconds.set.assert_called_once()
    mock_metrics.backup_retained_count.set.assert_called_once()


def test_report_success_with_notifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    backup_dir = tmp_path / "daily_20260329"
    backup_dir.mkdir()
    (backup_dir / "data.bin").write_bytes(b"x" * 512)

    mock_notifier = MagicMock()
    mock_notifier.notify_backup_success = AsyncMock(return_value=None)
    mgr._notifier = mock_notifier

    # Patch asyncio.run to avoid nested event loop issues
    asyncio_run_calls: list = []
    monkeypatch.setattr("asyncio.run", lambda coro: asyncio_run_calls.append(coro))

    mgr._report_success("daily_20260329", duration_s=1.0)

    assert len(asyncio_run_calls) == 1


def test_report_failure_no_notifier_no_crash(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    mgr._report_failure("daily_20260329", BackupError("disk full"), duration_s=0.5)


def test_report_failure_finds_last_success(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    # Create previous successful backup dirs
    (tmp_path / "daily_20260327").mkdir()
    (tmp_path / "daily_20260328").mkdir()

    with patch("hft_platform.ops.backup.logger") as mock_log:
        mgr._report_failure("daily_20260329", BackupError("boom"), duration_s=1.0)
        # The last_success should be max of existing dates
        call_kwargs = mock_log.warning.call_args[1]
        assert call_kwargs.get("last_success") == "20260328"


def test_report_failure_last_success_unknown_when_no_previous(tmp_path: Path) -> None:
    """When there are no prior backups, last_success is 'unknown'."""
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    with patch("hft_platform.ops.backup.logger") as mock_log:
        mgr._report_failure("daily_20260329", BackupError("err"), duration_s=0.5)
        call_kwargs = mock_log.warning.call_args[1]
        assert call_kwargs.get("last_success") == "unknown"


def test_report_failure_with_notifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    mock_notifier = MagicMock()
    mock_notifier.notify_backup_failed = AsyncMock(return_value=None)
    mgr._notifier = mock_notifier

    asyncio_run_calls: list = []
    monkeypatch.setattr("asyncio.run", lambda coro: asyncio_run_calls.append(coro))

    mgr._report_failure("daily_20260329", BackupError("err"), duration_s=0.5)

    assert len(asyncio_run_calls) == 1


def test_report_failure_with_metrics(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    mock_metrics = MagicMock()
    mgr._metrics = mock_metrics

    mgr._report_failure("daily_20260329", BackupError("err"), duration_s=0.7)

    mock_metrics.backup_duration_seconds.set.assert_called_once_with(pytest.approx(0.7))


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_invalid_backup_name_raises() -> None:
    mgr = _make_manager()
    with pytest.raises(ValueError, match="Invalid backup_name"):
        mgr.restore("not_a_valid_name")


def test_restore_invalid_target_db_raises() -> None:
    mgr = _make_manager()
    with pytest.raises(ValueError, match="Invalid target_db"):
        mgr.restore("daily_20260329", target_db="bad db!")


def test_restore_default_target_db() -> None:
    """restore with target_db='hft' uses non-AS SQL."""
    mgr = _make_manager()
    client = _mock_client()

    with _patch_client(mgr, client):
        mgr.restore("daily_20260329")

    sql_call = client.execute.call_args[0][0]
    assert "RESTORE DATABASE hft FROM" in sql_call
    assert " AS " not in sql_call


def test_restore_alternate_target_db() -> None:
    """restore with alternate target_db uses AS syntax."""
    mgr = _make_manager()
    client = _mock_client()

    with _patch_client(mgr, client):
        mgr.restore("daily_20260329", target_db="hft_test")

    sql_call = client.execute.call_args[0][0]
    assert "AS hft_test" in sql_call


# ---------------------------------------------------------------------------
# restore_table
# ---------------------------------------------------------------------------


def test_restore_table_invalid_name_raises() -> None:
    mgr = _make_manager()
    with pytest.raises(ValueError, match="Invalid backup_name"):
        mgr.restore_table("bad-name", table="market_data")


def test_restore_table_invalid_table_raises() -> None:
    mgr = _make_manager()
    with pytest.raises(ValueError, match="Invalid table"):
        mgr.restore_table("daily_20260329", table="bad table!")


def test_restore_table_invalid_target_db_raises() -> None:
    mgr = _make_manager()
    with pytest.raises(ValueError, match="Invalid target_db"):
        mgr.restore_table("daily_20260329", table="market_data", target_db="bad db!")


def test_restore_table_default_db() -> None:
    mgr = _make_manager()
    client = _mock_client()

    with _patch_client(mgr, client):
        mgr.restore_table("daily_20260329", table="market_data")

    sql_call = client.execute.call_args[0][0]
    assert "RESTORE TABLE hft.market_data FROM" in sql_call
    assert " AS " not in sql_call


def test_restore_table_alternate_db() -> None:
    mgr = _make_manager()
    client = _mock_client()

    with _patch_client(mgr, client):
        mgr.restore_table("daily_20260329", table="market_data", target_db="hft_test")

    sql_call = client.execute.call_args[0][0]
    assert "AS hft_test.market_data" in sql_call


# ---------------------------------------------------------------------------
# verify_restore
# ---------------------------------------------------------------------------


def test_verify_restore_success(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = _make_manager()
    client = _mock_client()

    table_list = [("market_data",), ("orders",)]
    # Per-table count queries
    client.execute.side_effect = [
        table_list,  # SELECT name FROM system.tables
        [(1000,)],   # orig count market_data
        [(1000,)],   # restored count market_data
        [(500,)],    # orig count orders
        [(500,)],    # restored count orders
        None,        # DROP DATABASE
    ]

    monkeypatch.setattr(BackupManager, "restore", lambda self, name, target_db: None)

    with _patch_client(mgr, client):
        results = mgr.verify_restore("daily_20260329")

    assert results["market_data"] == (1000, 1000)
    assert results["orders"] == (500, 500)


def test_verify_restore_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    mgr = _make_manager()
    client = _mock_client()

    table_list = [("market_data",)]
    client.execute.side_effect = [
        table_list,   # SELECT name FROM system.tables
        [(1000,)],    # orig count
        [(900,)],     # restored count — mismatch!
        None,         # DROP DATABASE
    ]

    monkeypatch.setattr(BackupManager, "restore", lambda self, name, target_db: None)

    with _patch_client(mgr, client):
        with pytest.raises(BackupError, match="mismatch"):
            mgr.verify_restore("daily_20260329")


def test_verify_restore_drops_temp_db_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Temp DB cleanup happens even when restore raises."""
    mgr = _make_manager()
    client = _mock_client()

    drop_calls: list[str] = []

    def _execute(sql, *args, **kwargs):
        if "DROP" in sql:
            drop_calls.append(sql)
            return None
        if "system.tables" in sql:
            return [("market_data",)]
        return [(100,)]

    client.execute.side_effect = _execute

    # restore itself raises an error
    monkeypatch.setattr(
        BackupManager,
        "restore",
        lambda self, name, target_db: (_ for _ in ()).throw(BackupError("fail")),
    )

    with _patch_client(mgr, client):
        with pytest.raises(BackupError):
            mgr.verify_restore("daily_20260329")

    # DROP should have been called in finally block
    assert any("DROP DATABASE" in c for c in drop_calls)


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


def test_list_backups_empty_dir(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    result = mgr.list_backups()
    assert result == []


def test_list_backups_nonexistent_dir() -> None:
    mgr = BackupManager(backup_dir="/nonexistent/path", retain_days=7, ch_host="localhost")
    result = mgr.list_backups()
    assert result == []


def test_list_backups_returns_matching_dirs(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    # Create valid backup dirs with files
    b1 = tmp_path / "daily_20260327"
    b1.mkdir()
    (b1 / "meta.json").write_bytes(b"x" * 100)

    b2 = tmp_path / "daily_20260328"
    b2.mkdir()
    (b2 / "meta.json").write_bytes(b"x" * 200)

    # Non-backup dir should be ignored
    other = tmp_path / "some_other_dir"
    other.mkdir()

    result = mgr.list_backups()

    assert len(result) == 2
    names = [r["name"] for r in result]
    assert "daily_20260327" in names
    assert "daily_20260328" in names
    assert "some_other_dir" not in names

    for r in result:
        assert "date" in r
        assert "size_bytes" in r
        assert "size_mb" in r


def test_list_backups_ignores_files(tmp_path: Path) -> None:
    """Files in backup root are skipped (only dirs counted)."""
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    (tmp_path / "daily_20260327").write_text("not a dir")  # file, not dir

    result = mgr.list_backups()
    assert result == []


# ---------------------------------------------------------------------------
# _backup_size_bytes / _count_retained
# ---------------------------------------------------------------------------


def test_backup_size_bytes_nonexistent_returns_zero(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    size = mgr._backup_size_bytes("daily_20260329")
    assert size == 0


def test_backup_size_bytes_calculates_correctly(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")
    backup_dir = tmp_path / "daily_20260329"
    backup_dir.mkdir()
    (backup_dir / "a.bin").write_bytes(b"x" * 500)
    (backup_dir / "b.bin").write_bytes(b"y" * 300)

    size = mgr._backup_size_bytes("daily_20260329")
    assert size == 800


def test_count_retained_counts_valid_dirs(tmp_path: Path) -> None:
    mgr = BackupManager(backup_dir=str(tmp_path), retain_days=7, ch_host="localhost")

    (tmp_path / "daily_20260327").mkdir()
    (tmp_path / "daily_20260328").mkdir()
    (tmp_path / "not_a_backup").mkdir()

    count = mgr._count_retained()
    assert count == 2


def test_count_retained_nonexistent_returns_zero() -> None:
    mgr = BackupManager(backup_dir="/nonexistent", retain_days=7, ch_host="localhost")
    assert mgr._count_retained() == 0


# ---------------------------------------------------------------------------
# _backup_name_re pattern validation
# ---------------------------------------------------------------------------


def test_backup_name_re_valid() -> None:
    assert _BACKUP_NAME_RE.match("daily_20260329") is not None
    assert _BACKUP_NAME_RE.match("daily_20261231") is not None


def test_backup_name_re_invalid() -> None:
    assert _BACKUP_NAME_RE.match("daily_2026032") is None  # 7 digits
    assert _BACKUP_NAME_RE.match("weekly_20260329") is None
    assert _BACKUP_NAME_RE.match("daily_abcdefgh") is None


# ---------------------------------------------------------------------------
# _execute_backup
# ---------------------------------------------------------------------------


def test_execute_backup_sends_correct_sql() -> None:
    mgr = _make_manager()
    client = _mock_client()

    with _patch_client(mgr, client):
        mgr._execute_backup("daily_20260329")

    sql_call = client.execute.call_args[0][0]
    assert "BACKUP DATABASE hft" in sql_call
    assert "daily_20260329/" in sql_call
    assert "backup_local" in sql_call


# ---------------------------------------------------------------------------
# _run_archive_hook (no-op by default)
# ---------------------------------------------------------------------------


def test_run_archive_hook_is_noop() -> None:
    mgr = _make_manager()
    mgr._run_archive_hook("daily_20260329")  # should not raise, return None
