"""Tests for go-live checklist CLI commands (WU-20)."""

import json
from argparse import Namespace

import pytest

from hft_platform.cli._golive import (
    _check_alertmanager_config,
    _check_config_not_sim,
    _check_disk_space,
    _check_kill_switch,
    _check_position_checkpoint,
    _check_wal_backlog,
    cmd_golive_check,
)


class TestCheckKillSwitch:
    def test_inactive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "missing"))
        result = _check_kill_switch()
        assert result["ok"] is True

    def test_active(self, tmp_path, monkeypatch):
        ks = tmp_path / "kill_switch"
        ks.write_text('{"reason": "test"}')
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(ks))
        result = _check_kill_switch()
        assert result["ok"] is False
        assert "deactivate" in result["detail"]


class TestCheckPositionCheckpoint:
    def test_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "missing.json"))
        result = _check_position_checkpoint()
        assert result["ok"] is False

    def test_exists(self, tmp_path, monkeypatch):
        path = tmp_path / "checkpoint.json"
        path.write_text("{}")
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(path))
        result = _check_position_checkpoint()
        assert result["ok"] is True


class TestCheckWalBacklog:
    def test_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "nonexistent"))
        result = _check_wal_backlog()
        assert result["ok"] is True

    def test_within_limit(self, tmp_path, monkeypatch):
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        for i in range(3):
            (wal_dir / f"seg_{i}.wal").write_text("")
        monkeypatch.setenv("HFT_WAL_DIR", str(wal_dir))
        result = _check_wal_backlog(max_files=10)
        assert result["ok"] is True

    def test_over_limit(self, tmp_path, monkeypatch):
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        for i in range(60):
            (wal_dir / f"seg_{i}.wal").write_text("")
        monkeypatch.setenv("HFT_WAL_DIR", str(wal_dir))
        result = _check_wal_backlog(max_files=50)
        assert result["ok"] is False


class TestCheckConfigNotSim:
    def test_sim_mode_fails(self, monkeypatch):
        monkeypatch.setenv("HFT_MODE", "sim")
        result = _check_config_not_sim()
        assert result["ok"] is False

    def test_live_mode_passes(self, monkeypatch):
        monkeypatch.setenv("HFT_MODE", "live")
        result = _check_config_not_sim()
        assert result["ok"] is True

    def test_default_is_sim(self, monkeypatch):
        monkeypatch.delenv("HFT_MODE", raising=False)
        result = _check_config_not_sim()
        assert result["ok"] is False


class TestCheckDiskSpace:
    def test_sufficient(self):
        result = _check_disk_space(min_gb=0.0)
        assert result["ok"] is True

    def test_insufficient(self):
        result = _check_disk_space(min_gb=999999.0)
        assert result["ok"] is False


class TestCheckAlertmanagerConfig:
    def test_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_ALERTMANAGER_CONFIG", str(tmp_path / "missing.yml"))
        result = _check_alertmanager_config()
        assert result["ok"] is False

    def test_exists(self, tmp_path, monkeypatch):
        path = tmp_path / "alertmanager.yml"
        path.write_text("route: {}")
        monkeypatch.setenv("HFT_ALERTMANAGER_CONFIG", str(path))
        result = _check_alertmanager_config()
        assert result["ok"] is True


class TestCmdGoliveCheck:
    def test_json_output(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_MODE", "live")
        monkeypatch.setenv("HFT_ALERTMANAGER_CONFIG", str(tmp_path / "missing"))
        args = Namespace(skip=[], json=True)
        with pytest.raises(SystemExit) as exc_info:
            cmd_golive_check(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "ok" in data
        assert "checks" in data
        assert len(data["checks"]) == 6

    def test_skip_checks(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_MODE", "live")
        monkeypatch.setenv("HFT_ALERTMANAGER_CONFIG", str(tmp_path / "missing"))
        args = Namespace(skip=["position_checkpoint", "alertmanager_config"], json=True)
        with pytest.raises(SystemExit):
            cmd_golive_check(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        skipped = [c for c in data["checks"] if c["detail"] == "SKIPPED"]
        assert len(skipped) == 2

    def test_human_output(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "missing"))
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_ALERTMANAGER_CONFIG", str(tmp_path / "missing"))
        args = Namespace(skip=[], json=False)
        with pytest.raises(SystemExit):
            cmd_golive_check(args)
        out = capsys.readouterr().out
        assert "Go-live:" in out
