"""Tests for CLI risk kill switch commands (WU-06)."""

import json
import os
from argparse import Namespace

import pytest

from hft_platform.cli._risk import (
    _get_kill_switch_path,
    cmd_risk_halt,
    cmd_risk_resume,
    cmd_risk_status,
)


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
        args = Namespace(reason="test halt")
        cmd_risk_halt(args)
        assert os.path.exists(kill_switch_dir)
        with open(kill_switch_dir) as f:
            data = json.load(f)
        assert data["reason"] == "test halt"
        assert data["actor"] == "cli"
        assert "timestamp_ns" in data

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        ks_path = str(tmp_path / "nested" / "dir" / "kill_switch")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)
        args = Namespace(reason="nested test")
        cmd_risk_halt(args)
        assert os.path.exists(ks_path)

    def test_overwrites_existing(self, kill_switch_dir):
        args = Namespace(reason="first")
        cmd_risk_halt(args)
        args2 = Namespace(reason="second")
        cmd_risk_halt(args2)
        with open(kill_switch_dir) as f:
            data = json.load(f)
        assert data["reason"] == "second"


class TestCmdRiskResume:
    def test_removes_kill_switch_file(self, kill_switch_dir, capsys):
        # Create file first
        os.makedirs(os.path.dirname(kill_switch_dir) or ".", exist_ok=True)
        with open(kill_switch_dir, "w") as f:
            json.dump({"reason": "test"}, f)
        args = Namespace()
        cmd_risk_resume(args)
        assert not os.path.exists(kill_switch_dir)
        out = capsys.readouterr().out
        assert "DEACTIVATED" in out

    def test_no_file_prints_message(self, kill_switch_dir, capsys):
        args = Namespace()
        cmd_risk_resume(args)
        out = capsys.readouterr().out
        assert "No kill switch file" in out


class TestCmdRiskStatus:
    def test_active_status(self, kill_switch_dir, capsys):
        os.makedirs(os.path.dirname(kill_switch_dir) or ".", exist_ok=True)
        with open(kill_switch_dir, "w") as f:
            json.dump({"reason": "testing", "actor": "cli", "timestamp_ns": 123}, f)
        args = Namespace()
        cmd_risk_status(args)
        out = capsys.readouterr().out
        assert "ACTIVE" in out
        assert "testing" in out

    def test_inactive_status(self, kill_switch_dir, capsys):
        args = Namespace()
        cmd_risk_status(args)
        out = capsys.readouterr().out
        assert "INACTIVE" in out

    def test_corrupt_file(self, kill_switch_dir, capsys):
        os.makedirs(os.path.dirname(kill_switch_dir) or ".", exist_ok=True)
        with open(kill_switch_dir, "w") as f:
            f.write("not json")
        args = Namespace()
        cmd_risk_status(args)
        out = capsys.readouterr().out
        assert "ACTIVE (file corrupt)" in out
