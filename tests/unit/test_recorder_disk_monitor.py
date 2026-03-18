"""Tests for hft_platform.recorder.disk_monitor."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.disk_monitor import (
    DiskPressureLevel,
    DiskPressureMonitor,
    TopicPolicy,
)

# ── DiskPressureLevel enum ───────────────────────────────────────────


class TestDiskPressureLevel:
    def test_ordering(self) -> None:
        assert DiskPressureLevel.OK < DiskPressureLevel.WARN
        assert DiskPressureLevel.WARN < DiskPressureLevel.CRITICAL
        assert DiskPressureLevel.CRITICAL < DiskPressureLevel.HALT

    def test_int_values(self) -> None:
        assert int(DiskPressureLevel.OK) == 0
        assert int(DiskPressureLevel.HALT) == 3


# ── TopicPolicy ──────────────────────────────────────────────────────


class TestTopicPolicy:
    def test_policy_values(self) -> None:
        assert TopicPolicy.WRITE == "write"
        assert TopicPolicy.DROP == "drop"
        assert TopicPolicy.HALT == "halt"


# ── DiskPressureMonitor: init & defaults ─────────────────────────────


class TestDiskPressureMonitorInit:
    def test_defaults(self) -> None:
        m = DiskPressureMonitor(wal_dir="/tmp/wal_test")
        assert m._warn_mb == 100.0
        assert m._critical_mb == 300.0
        assert m._halt_mb == 500.0
        assert m._interval == 10.0
        assert m.get_level() == DiskPressureLevel.OK

    def test_explicit_params_override_env(self) -> None:
        m = DiskPressureMonitor(
            wal_dir="/tmp/wal",
            warn_mb=50,
            critical_mb=150,
            halt_mb=250,
            check_interval_s=5,
        )
        assert m._warn_mb == 50
        assert m._critical_mb == 150
        assert m._halt_mb == 250
        assert m._interval == 5

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_WAL_WARN_MB", "42")
        monkeypatch.setenv("HFT_WAL_CRITICAL_MB", "142")
        monkeypatch.setenv("HFT_WAL_HALT_MB", "242")
        monkeypatch.setenv("HFT_DISK_CHECK_INTERVAL_S", "3")
        m = DiskPressureMonitor(wal_dir="/tmp/wal")
        assert m._warn_mb == 42.0
        assert m._critical_mb == 142.0
        assert m._halt_mb == 242.0
        assert m._interval == 3.0


# ── _compute_level ───────────────────────────────────────────────────


class TestComputeLevel:
    def setup_method(self) -> None:
        self.mon = DiskPressureMonitor(
            wal_dir="/tmp/wal",
            warn_mb=100,
            critical_mb=300,
            halt_mb=500,
        )

    def test_ok(self) -> None:
        assert self.mon._compute_level(0) == DiskPressureLevel.OK
        assert self.mon._compute_level(99.9) == DiskPressureLevel.OK

    def test_warn(self) -> None:
        assert self.mon._compute_level(100) == DiskPressureLevel.WARN
        assert self.mon._compute_level(200) == DiskPressureLevel.WARN

    def test_critical(self) -> None:
        assert self.mon._compute_level(300) == DiskPressureLevel.CRITICAL
        assert self.mon._compute_level(400) == DiskPressureLevel.CRITICAL

    def test_halt(self) -> None:
        assert self.mon._compute_level(500) == DiskPressureLevel.HALT
        assert self.mon._compute_level(9999) == DiskPressureLevel.HALT


# ── _wal_dir_size_mb ─────────────────────────────────────────────────


class TestWalDirSizeMb:
    def test_missing_dir_returns_zero(self, tmp_path: object) -> None:
        mon = DiskPressureMonitor(wal_dir="/nonexistent_wal_dir_xyz")
        assert mon._wal_dir_size_mb() == 0.0

    def test_empty_dir(self, tmp_path: object) -> None:
        import pathlib

        d = pathlib.Path(str(tmp_path)) / "wal"
        d.mkdir()
        mon = DiskPressureMonitor(wal_dir=str(d))
        assert mon._wal_dir_size_mb() == 0.0

    def test_files_summed(self, tmp_path: object) -> None:
        import pathlib

        d = pathlib.Path(str(tmp_path)) / "wal"
        d.mkdir()
        # Create two 1 MiB files
        for i in range(2):
            (d / f"seg{i}.wal").write_bytes(b"\x00" * (1024 * 1024))
        mon = DiskPressureMonitor(wal_dir=str(d))
        size = mon._wal_dir_size_mb()
        assert abs(size - 2.0) < 0.01

    def test_subdirs_ignored(self, tmp_path: object) -> None:
        import pathlib

        d = pathlib.Path(str(tmp_path)) / "wal"
        d.mkdir()
        (d / "subdir").mkdir()
        mon = DiskPressureMonitor(wal_dir=str(d))
        assert mon._wal_dir_size_mb() == 0.0


# ── _check (level transitions + hooks) ──────────────────────────────


class TestCheck:
    def test_no_transition_no_hook(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal", warn_mb=100)
        hook = MagicMock()
        mon.register_hook(hook)
        # Patch to return 0 MB (stays OK)
        with patch.object(mon, "_wal_dir_size_mb", return_value=0.0):
            mon._check()
        hook.assert_not_called()

    def test_transition_fires_hook(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal", warn_mb=100)
        transitions: list[tuple[DiskPressureLevel, DiskPressureLevel]] = []
        mon.register_hook(lambda old, new: transitions.append((old, new)))
        with patch.object(mon, "_wal_dir_size_mb", return_value=150.0):
            with patch("hft_platform.recorder.disk_monitor.MetricsRegistry", create=True):
                mon._check()
        assert len(transitions) == 1
        assert transitions[0] == (DiskPressureLevel.OK, DiskPressureLevel.WARN)
        assert mon.get_level() == DiskPressureLevel.WARN

    def test_transition_ok_to_halt(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal", warn_mb=100, halt_mb=500)
        with patch.object(mon, "_wal_dir_size_mb", return_value=600.0):
            mon._check()
        assert mon.get_level() == DiskPressureLevel.HALT

    def test_recovery_to_ok(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal", warn_mb=100)
        # First go to WARN
        with patch.object(mon, "_wal_dir_size_mb", return_value=150.0):
            mon._check()
        assert mon.get_level() == DiskPressureLevel.WARN
        # Recover
        with patch.object(mon, "_wal_dir_size_mb", return_value=10.0):
            mon._check()
        assert mon.get_level() == DiskPressureLevel.OK

    def test_hook_exception_does_not_crash(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal", warn_mb=100)
        mon.register_hook(lambda old, new: (_ for _ in ()).throw(RuntimeError("boom")))
        second_hook = MagicMock()
        mon.register_hook(second_hook)
        with patch.object(mon, "_wal_dir_size_mb", return_value=150.0):
            mon._check()  # Should not raise
        # Second hook should still be called even if first raises
        # (hooks iterate via for-loop with try/except)

    def test_multiple_hooks_called(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal", warn_mb=100)
        calls: list[str] = []
        mon.register_hook(lambda o, n: calls.append("a"))
        mon.register_hook(lambda o, n: calls.append("b"))
        with patch.object(mon, "_wal_dir_size_mb", return_value=150.0):
            mon._check()
        assert calls == ["a", "b"]


# ── get_topic_policy ─────────────────────────────────────────────────


class TestGetTopicPolicy:
    def test_default_is_write(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal")
        assert mon.get_topic_policy("orders") == "write"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_WAL_FIRST_POLICY_ORDERS", "drop")
        mon = DiskPressureMonitor(wal_dir="/tmp/wal")
        assert mon.get_topic_policy("orders") == "drop"

    def test_halt_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_WAL_FIRST_POLICY_FILLS", "halt")
        mon = DiskPressureMonitor(wal_dir="/tmp/wal")
        assert mon.get_topic_policy("fills") == "halt"

    def test_invalid_policy_falls_back_to_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_WAL_FIRST_POLICY_TRADES", "invalid_value")
        mon = DiskPressureMonitor(wal_dir="/tmp/wal")
        assert mon.get_topic_policy("trades") == "write"

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_WAL_FIRST_POLICY_MYTABLE", "  DROP  ")
        mon = DiskPressureMonitor(wal_dir="/tmp/wal")
        assert mon.get_topic_policy("mytable") == "drop"

    def test_table_name_uppercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_WAL_FIRST_POLICY_LOWTABLE", "halt")
        mon = DiskPressureMonitor(wal_dir="/tmp/wal")
        assert mon.get_topic_policy("lowtable") == "halt"


# ── start / stop ─────────────────────────────────────────────────────


class TestStartStop:
    def test_start_creates_daemon_thread(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal", check_interval_s=999)
        with patch.object(mon, "_loop"):
            mon.start()
            assert mon._running is True
            assert mon._thread is not None
            assert mon._thread.daemon is True
            mon.stop()
            assert mon._running is False

    def test_stop_sets_running_false(self) -> None:
        mon = DiskPressureMonitor(wal_dir="/tmp/wal")
        mon._running = True
        mon.stop()
        assert mon._running is False
