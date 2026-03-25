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
