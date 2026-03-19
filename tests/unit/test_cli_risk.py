"""Tests for CLI risk kill switch commands (WU-06)."""
import json, os
from argparse import Namespace
import pytest
from hft_platform.cli._risk import _get_kill_switch_path, cmd_risk_halt, cmd_risk_resume, cmd_risk_status

@pytest.fixture()
def kill_switch_dir(tmp_path, monkeypatch):
    ks_path = str(tmp_path / "kill_switch")
    monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)
    return ks_path

class TestGetKillSwitchPath:
    def test_default_path(self, monkeypatch):
        monkeypatch.delenv("HFT_KILL_SWITCH_PATH", raising=False)
        assert _get_kill_switch_path() == ".runtime/kill_switch"
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", "/custom/path")
        assert _get_kill_switch_path() == "/custom/path"

class TestCmdRiskHalt:
    def test_creates_kill_switch_file(self, kill_switch_dir):
        cmd_risk_halt(Namespace(reason="test halt"))
        assert os.path.exists(kill_switch_dir)
        with open(kill_switch_dir) as f:
            data = json.load(f)
        assert data["reason"] == "test halt"
        assert data["actor"] == "cli"
    def test_overwrites_existing(self, kill_switch_dir):
        cmd_risk_halt(Namespace(reason="first"))
        cmd_risk_halt(Namespace(reason="second"))
        with open(kill_switch_dir) as f:
            data = json.load(f)
        assert data["reason"] == "second"

class TestCmdRiskResume:
    def test_removes_kill_switch_file(self, kill_switch_dir, capsys):
        os.makedirs(os.path.dirname(kill_switch_dir) or ".", exist_ok=True)
        with open(kill_switch_dir, "w") as f:
            json.dump({"reason": "test"}, f)
        cmd_risk_resume(Namespace())
        assert not os.path.exists(kill_switch_dir)
    def test_no_file_prints_message(self, kill_switch_dir, capsys):
        cmd_risk_resume(Namespace())
        assert "No kill switch file" in capsys.readouterr().out

class TestCmdRiskStatus:
    def test_active_status(self, kill_switch_dir, capsys):
        os.makedirs(os.path.dirname(kill_switch_dir) or ".", exist_ok=True)
        with open(kill_switch_dir, "w") as f:
            json.dump({"reason": "testing", "actor": "cli", "timestamp_ns": 123}, f)
        cmd_risk_status(Namespace())
        assert "ACTIVE" in capsys.readouterr().out
    def test_inactive_status(self, kill_switch_dir, capsys):
        cmd_risk_status(Namespace())
        assert "INACTIVE" in capsys.readouterr().out
    def test_corrupt_file(self, kill_switch_dir, capsys):
        os.makedirs(os.path.dirname(kill_switch_dir) or ".", exist_ok=True)
        with open(kill_switch_dir, "w") as f:
            f.write("not json")
        cmd_risk_status(Namespace())
        assert "ACTIVE (file corrupt)" in capsys.readouterr().out
