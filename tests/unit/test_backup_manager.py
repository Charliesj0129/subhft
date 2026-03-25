"""Tests for BackupManager."""

from __future__ import annotations

import os
from unittest.mock import patch


def test_backup_error_is_exception():
    from hft_platform.ops.backup import BackupError

    assert issubclass(BackupError, Exception)
    err = BackupError("disk full")
    assert str(err) == "disk full"


def test_constructor_defaults_from_env():
    with patch.dict(
        os.environ,
        {
            "HFT_CLICKHOUSE_HOST": "ch-host",
            "HFT_CLICKHOUSE_PORT": "9999",
            "HFT_CLICKHOUSE_USER": "admin",
            "HFT_CLICKHOUSE_PASSWORD": "secret",
        },
    ):
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


def test_cleanup_removes_old_backups(tmp_path):
    from hft_platform.ops.backup import BackupManager

    # Create fake backup dirs: 3 old + 1 recent
    for name in ["daily_20260213", "daily_20260218", "daily_20260222", "daily_20260324"]:
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
