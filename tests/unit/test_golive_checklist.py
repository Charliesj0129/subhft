"""Tests for go-live checklist CLI commands (WU-20)."""

import json
from argparse import Namespace

import pytest

from hft_platform.cli._golive import _check_config_not_sim, _check_kill_switch, cmd_golive_check


class TestCheckKillSwitch:
    def test_inactive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "m"))
        assert _check_kill_switch()["ok"] is True

    def test_active(self, tmp_path, monkeypatch):
        p = tmp_path / "ks"
        p.write_text("{}")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(p))
        assert _check_kill_switch()["ok"] is False


class TestCheckConfigNotSim:
    def test_sim_fails(self, monkeypatch):
        monkeypatch.setenv("HFT_MODE", "sim")
        assert _check_config_not_sim()["ok"] is False

    def test_live_passes(self, monkeypatch):
        monkeypatch.setenv("HFT_MODE", "live")
        assert _check_config_not_sim()["ok"] is True


class TestCmdGoliveCheck:
    def test_json_output(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_MODE", "live")
        monkeypatch.setenv("HFT_ALERTMANAGER_CONFIG", str(tmp_path / "m"))
        with pytest.raises(SystemExit):
            cmd_golive_check(Namespace(skip=[], json=True))
        data = json.loads(capsys.readouterr().out)
        assert "ok" in data and len(data["checks"]) == 6

    def test_skip(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_CHECKPOINT_PATH", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_WAL_DIR", str(tmp_path / "m"))
        monkeypatch.setenv("HFT_MODE", "live")
        monkeypatch.setenv("HFT_ALERTMANAGER_CONFIG", str(tmp_path / "m"))
        with pytest.raises(SystemExit):
            cmd_golive_check(Namespace(skip=["position_checkpoint"], json=True))
        data = json.loads(capsys.readouterr().out)
        assert sum(1 for c in data["checks"] if c["detail"] == "SKIPPED") == 1
