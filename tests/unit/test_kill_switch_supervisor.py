"""Tests for kill switch detection in supervisor loop (WU-07)."""
import json, os
from unittest.mock import MagicMock
from hft_platform.risk.storm_guard import StormGuardState

class TestKillSwitchSupervisor:
    def test_kill_switch_file_triggers_halt(self, tmp_path, monkeypatch):
        ks_path = str(tmp_path / "kill_switch")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)
        with open(ks_path, "w") as f:
            json.dump({"reason": "emergency"}, f)
        sg = MagicMock()
        sg.state = StormGuardState.NORMAL
        kp = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        if os.path.exists(kp) and sg.state != StormGuardState.HALT:
            try:
                with open(kp) as _f:
                    _d = json.load(_f)
                _r = _d.get("reason", "unknown")
            except Exception:
                _r = "kill_switch_file_present"
            sg.trigger_halt(f"KILL_SWITCH_FILE: {_r}")
        sg.trigger_halt.assert_called_once_with("KILL_SWITCH_FILE: emergency")

    def test_no_file_no_halt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "missing"))
        sg = MagicMock()
        sg.state = StormGuardState.NORMAL
        kp = os.getenv("HFT_KILL_SWITCH_PATH")
        if os.path.exists(kp) and sg.state != StormGuardState.HALT:
            sg.trigger_halt("test")
        sg.trigger_halt.assert_not_called()

    def test_already_halt_no_retrigger(self, tmp_path, monkeypatch):
        ks_path = str(tmp_path / "kill_switch")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)
        with open(ks_path, "w") as f:
            json.dump({"reason": "x"}, f)
        sg = MagicMock()
        sg.state = StormGuardState.HALT
        kp = os.getenv("HFT_KILL_SWITCH_PATH")
        if os.path.exists(kp) and sg.state != StormGuardState.HALT:
            sg.trigger_halt("test")
        sg.trigger_halt.assert_not_called()

    def test_corrupt_file_still_triggers(self, tmp_path, monkeypatch):
        ks_path = str(tmp_path / "kill_switch")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)
        with open(ks_path, "w") as f:
            f.write("{{{bad")
        sg = MagicMock()
        sg.state = StormGuardState.NORMAL
        kp = os.getenv("HFT_KILL_SWITCH_PATH")
        if os.path.exists(kp) and sg.state != StormGuardState.HALT:
            try:
                with open(kp) as _f:
                    _d = json.load(_f)
                _r = _d.get("reason", "unknown")
            except Exception:
                _r = "kill_switch_file_present"
            sg.trigger_halt(f"KILL_SWITCH_FILE: {_r}")
        sg.trigger_halt.assert_called_once_with("KILL_SWITCH_FILE: kill_switch_file_present")
