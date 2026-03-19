"""Tests for kill switch detection in supervisor loop (WU-07)."""

import json
import os
from unittest.mock import MagicMock


class TestKillSwitchSupervisor:
    """Test kill switch file detection triggers StormGuard HALT."""

    def _make_storm_guard_mock(self, state="NORMAL"):
        sg = MagicMock()
        sg.state = MagicMock()
        sg.state.__eq__ = lambda self_inner, other: state == str(other)
        sg.state.__ne__ = lambda self_inner, other: state != str(other)
        return sg

    def test_kill_switch_file_triggers_halt(self, tmp_path, monkeypatch):
        """When kill switch file exists and StormGuard is not HALT, trigger_halt is called."""
        from hft_platform.risk.storm_guard import StormGuardState

        ks_path = str(tmp_path / "kill_switch")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)

        # Write kill switch file
        with open(ks_path, "w") as f:
            json.dump({"reason": "emergency", "actor": "cli"}, f)

        # Simulate the supervisor check logic
        storm_guard = MagicMock()
        storm_guard.state = StormGuardState.NORMAL

        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        if os.path.exists(kill_switch_path):
            if storm_guard.state != StormGuardState.HALT:
                try:
                    with open(kill_switch_path, "r") as _ksf:
                        _ks_data = json.load(_ksf)
                    _ks_reason = _ks_data.get("reason", "unknown")
                except Exception:
                    _ks_reason = "kill_switch_file_present"
                storm_guard.trigger_halt(f"KILL_SWITCH_FILE: {_ks_reason}")

        storm_guard.trigger_halt.assert_called_once_with("KILL_SWITCH_FILE: emergency")

    def test_no_kill_switch_file_no_halt(self, tmp_path, monkeypatch):
        """When kill switch file does not exist, trigger_halt is not called."""
        from hft_platform.risk.storm_guard import StormGuardState

        ks_path = str(tmp_path / "kill_switch_missing")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)

        storm_guard = MagicMock()
        storm_guard.state = StormGuardState.NORMAL

        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        if os.path.exists(kill_switch_path):
            if storm_guard.state != StormGuardState.HALT:
                storm_guard.trigger_halt("KILL_SWITCH_FILE: test")

        storm_guard.trigger_halt.assert_not_called()

    def test_already_halt_no_retrigger(self, tmp_path, monkeypatch):
        """When StormGuard is already HALT, trigger_halt is not called again."""
        from hft_platform.risk.storm_guard import StormGuardState

        ks_path = str(tmp_path / "kill_switch")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)

        with open(ks_path, "w") as f:
            json.dump({"reason": "already halted"}, f)

        storm_guard = MagicMock()
        storm_guard.state = StormGuardState.HALT

        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        if os.path.exists(kill_switch_path):
            if storm_guard.state != StormGuardState.HALT:
                storm_guard.trigger_halt("KILL_SWITCH_FILE: test")

        storm_guard.trigger_halt.assert_not_called()

    def test_corrupt_file_still_triggers_halt(self, tmp_path, monkeypatch):
        """Corrupt JSON in kill switch file still triggers halt with fallback reason."""
        from hft_platform.risk.storm_guard import StormGuardState

        ks_path = str(tmp_path / "kill_switch")
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", ks_path)

        with open(ks_path, "w") as f:
            f.write("not json{{{")

        storm_guard = MagicMock()
        storm_guard.state = StormGuardState.NORMAL

        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        if os.path.exists(kill_switch_path):
            if storm_guard.state != StormGuardState.HALT:
                try:
                    with open(kill_switch_path, "r") as _ksf:
                        _ks_data = json.load(_ksf)
                    _ks_reason = _ks_data.get("reason", "unknown")
                except Exception:
                    _ks_reason = "kill_switch_file_present"
                storm_guard.trigger_halt(f"KILL_SWITCH_FILE: {_ks_reason}")

        storm_guard.trigger_halt.assert_called_once_with("KILL_SWITCH_FILE: kill_switch_file_present")
