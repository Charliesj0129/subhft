"""Tests for CLI health preflight commands (WU-13)."""

import json
from argparse import Namespace

import pytest

from hft_platform.cli._health import (
    _check_checkpoint_recent,
    _check_disk_space,
    _check_kill_switch,
    _check_wal_backlog,
    cmd_health_preflight,
)


class TestCheckCheckpointRecent:
    def test_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "missing.json"))
        assert _check_checkpoint_recent()["ok"] is False

    def test_recent(self, tmp_path, monkeypatch):
        p = tmp_path / "cp.json"
        p.write_text("{}")
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(p))
        assert _check_checkpoint_recent(max_age_s=60.0)["ok"] is True


class TestCheckWalBacklog:
    def test_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "x"))
        assert _check_wal_backlog()["ok"] is True


class TestCheckDiskSpace:
    def test_sufficient(self):
        assert _check_disk_space(min_gb=0.0)["ok"] is True

    def test_insufficient(self):
        assert _check_disk_space(min_gb=999999.0)["ok"] is False


class TestCheckKillSwitch:
    def test_no_ks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "m"))
        assert _check_kill_switch()["ok"] is True

    def test_active_ks(self, tmp_path, monkeypatch):
        p = tmp_path / "ks"
        p.write_text("{}")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(p))
        assert _check_kill_switch()["ok"] is False


class TestCmdHealthPreflight:
    def test_json_output(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "m"))
        with pytest.raises(SystemExit):
            cmd_health_preflight(Namespace(timeout=0.5, json=True))
        data = json.loads(capsys.readouterr().out)
        assert "ok" in data and len(data["checks"]) == 7
