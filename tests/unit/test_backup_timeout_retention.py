"""Regression tests: a backup that outlives the client must not disable retention.

2026-10-13..10-21 on THESHOW: ``BACKUP DATABASE hft`` grew past the HTTP
client's 300 s read timeout. ``client.command`` raised clickhouse-connect's
``OperationalError``, which ``run_daily`` did not catch (it caught only
``BackupError``), so ``_cleanup_old_backups`` and ``_report_failure`` were
never reached. ClickHouse finished every backup server-side; nine full
backups (13-16 GiB each) piled up and the 10-21 backup filled the disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.ops.backup import _TZ_TAIPEI, BackupError, BackupManager

_GIB = 1024**3


class ClientReadTimeout(Exception):
    """Stands in for clickhouse_connect OperationalError: not a BackupError."""


def _name(days_ago: int) -> str:
    return f"daily_{(datetime.now(tz=_TZ_TAIPEI) - timedelta(days=days_ago)).strftime('%Y%m%d')}"


def _disk(name: str) -> str:
    return f"Disk('backup_local', '{name}/')"


def _make_manager(backup_dir: Path, **kwargs) -> BackupManager:
    return BackupManager(
        retain_days=3,
        backup_dir=str(backup_dir),
        ch_host="localhost",
        ch_port=8123,
        ch_user="default",
        ch_password="",
        **kwargs,
    )


def _seed(backup_dir: Path, *names: str) -> None:
    for name in names:
        (backup_dir / name).mkdir()
        (backup_dir / name / "data.bin").write_bytes(b"x")


def _client(history: list[tuple[str, str]], hft_bytes: int = 1 * _GIB) -> MagicMock:
    """Mock client: system.backups returns ``history`` (name, status) oldest first."""
    client = MagicMock()

    def query(sql, parameters=None):
        result = MagicMock()
        if "FROM system.backups" in sql and "GROUP BY" not in sql and "WHERE name" not in sql:
            result.result_rows = [(_disk(n), s) for n, s in history]
        elif "FROM system.parts" in sql:
            result.result_rows = [(hft_bytes,)]
        else:
            result.result_rows = []
        return result

    client.query.side_effect = query
    return client


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_BACKUP_ENABLED", "1")
    monkeypatch.setattr(BackupManager, "_check_disk_registered", lambda self: None)
    monkeypatch.setattr(BackupManager, "_abort_stale_backups", lambda self: None)


def test_run_daily_client_timeout_is_reported_as_failure(enabled, monkeypatch, tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr(BackupManager, "_make_room", lambda self: None, raising=False)

    def timed_out(self, name):
        raise ClientReadTimeout("Read timed out. (read timeout=300)")

    monkeypatch.setattr(BackupManager, "_execute_backup", timed_out)
    reported: list[Exception] = []
    monkeypatch.setattr(BackupManager, "_report_failure", lambda self, name, err, dur: reported.append(err))

    assert mgr.run_daily() is False
    assert len(reported) == 1
    assert isinstance(reported[0], ClientReadTimeout)


def test_run_daily_client_timeout_still_prunes_old_backups(enabled, monkeypatch, tmp_path: Path) -> None:
    old = [_name(13), _name(10), _name(6)]
    recent = [_name(2), _name(1)]
    _seed(tmp_path, *old, *recent)
    history = [(n, "BACKUP_CREATED") for n in [*old, *recent]]
    mgr = _make_manager(tmp_path)

    def timed_out(self, name):
        raise ClientReadTimeout("Read timed out. (read timeout=300)")

    monkeypatch.setattr(BackupManager, "_execute_backup", timed_out)
    monkeypatch.setattr(BackupManager, "_report_failure", lambda self, name, err, dur: None)
    with patch.object(BackupManager, "_client", return_value=_client(history)):
        assert mgr.run_daily() is False

    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(recent)


def test_cleanup_keeps_newest_complete_backup_when_every_backup_is_old(tmp_path: Path) -> None:
    names = [_name(20), _name(15), _name(9)]
    _seed(tmp_path, *names)
    mgr = _make_manager(tmp_path)

    mgr._cleanup_old_backups(keep=frozenset({_name(9)}))

    assert [p.name for p in tmp_path.iterdir()] == [_name(9)]


def test_make_room_removes_failed_debris_but_keeps_a_name_that_once_succeeded(monkeypatch, tmp_path: Path) -> None:
    debris = _name(1)
    rerun = _name(2)  # created at 06:30, then a manual rerun failed with BACKUP_ALREADY_EXISTS
    _seed(tmp_path, debris, rerun)
    history = [(rerun, "BACKUP_CREATED"), (rerun, "BACKUP_FAILED"), (debris, "BACKUP_FAILED")]
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr("hft_platform.ops.backup.shutil.disk_usage", lambda p: MagicMock(free=500 * _GIB))

    with patch.object(BackupManager, "_client", return_value=_client(history)):
        mgr._make_room()

    assert [p.name for p in tmp_path.iterdir()] == [rerun]


def test_make_room_refuses_backup_without_space_for_it(enabled, monkeypatch, tmp_path: Path) -> None:
    _seed(tmp_path, _name(1))
    history = [(_name(1), "BACKUP_CREATED")]
    mgr = _make_manager(tmp_path, min_free_after_bytes=10 * _GIB)
    # 17 GiB of data needs 17 + 10 GiB free; only 3.3 GiB is (THESHOW, 2026-10-22).
    monkeypatch.setattr("hft_platform.ops.backup.shutil.disk_usage", lambda p: MagicMock(free=int(3.3 * _GIB)))
    started: list[str] = []
    monkeypatch.setattr(BackupManager, "_execute_backup", lambda self, name: started.append(name))
    reported: list[Exception] = []
    monkeypatch.setattr(BackupManager, "_report_failure", lambda self, name, err, dur: reported.append(err))

    with patch.object(BackupManager, "_client", return_value=_client(history, hft_bytes=17 * _GIB)):
        assert mgr.run_daily() is False

    assert started == []
    assert isinstance(reported[0], BackupError)
    assert "free space" in str(reported[0])


def test_execute_backup_runs_async_and_waits_for_terminal_status(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, poll_interval_s=0.0)
    client = MagicMock()
    statuses = iter(["CREATING_BACKUP", "CREATING_BACKUP", "BACKUP_CREATED"])

    def query(sql, parameters=None):
        result = MagicMock()
        result.result_rows = [(next(statuses), "")]
        return result

    client.query.side_effect = query
    with patch.object(BackupManager, "_client", return_value=client):
        mgr._execute_backup("daily_20261022")

    sql = client.command.call_args[0][0]
    assert sql.rstrip().endswith("ASYNC")
    assert client.query.call_count == 3


def test_execute_backup_gives_up_at_deadline(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, backup_timeout_s=0.0, poll_interval_s=0.0)
    client = MagicMock()
    still_running = MagicMock()
    still_running.result_rows = [("CREATING_BACKUP", "")]
    client.query.return_value = still_running

    with patch.object(BackupManager, "_client", return_value=client):
        with pytest.raises(BackupError, match="CREATING_BACKUP"):
            mgr._execute_backup("daily_20261022")
