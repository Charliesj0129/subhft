"""Tests for CE3-05: DiskPressureMonitor."""
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from hft_platform.recorder.disk_monitor import DiskPressureLevel, DiskPressureMonitor


def _make_monitor(warn_mb=10, critical_mb=20, halt_mb=30, interval_s=0.05, tmpdir=None):
    wal_dir = tmpdir or tempfile.mkdtemp()
    return DiskPressureMonitor(
        wal_dir=wal_dir,
        warn_mb=warn_mb,
        critical_mb=critical_mb,
        halt_mb=halt_mb,
        check_interval_s=interval_s,
    )


def test_initial_level_ok():
    mon = _make_monitor()
    assert mon.get_level() == DiskPressureLevel.OK


def test_compute_level_ok():
    mon = _make_monitor(warn_mb=100, critical_mb=200, halt_mb=300)
    assert mon._compute_level(0) == DiskPressureLevel.OK
    assert mon._compute_level(50) == DiskPressureLevel.OK


def test_compute_level_warn():
    mon = _make_monitor(warn_mb=100, critical_mb=200, halt_mb=300)
    assert mon._compute_level(100) == DiskPressureLevel.WARN
    assert mon._compute_level(150) == DiskPressureLevel.WARN


def test_compute_level_critical():
    mon = _make_monitor(warn_mb=100, critical_mb=200, halt_mb=300)
    assert mon._compute_level(200) == DiskPressureLevel.CRITICAL
    assert mon._compute_level(250) == DiskPressureLevel.CRITICAL


def test_compute_level_halt():
    mon = _make_monitor(warn_mb=100, critical_mb=200, halt_mb=300)
    assert mon._compute_level(300) == DiskPressureLevel.HALT
    assert mon._compute_level(999) == DiskPressureLevel.HALT


def test_hook_called_on_transition():
    with tempfile.TemporaryDirectory() as tmpdir:
        mon = _make_monitor(warn_mb=0.001, critical_mb=100, halt_mb=200, interval_s=0.05, tmpdir=tmpdir)

        transitions = []
        mon.register_hook(lambda old, new: transitions.append((old, new)))

        # Write a file to push WAL above 0.001 MB
        with open(os.path.join(tmpdir, "test.jsonl"), "wb") as f:
            f.write(b"x" * 2000)  # ~2 KB

        mon.start()
        time.sleep(0.2)
        mon.stop()

        # Should have transitioned to at least WARN
        assert any(new >= DiskPressureLevel.WARN for _, new in transitions)


def test_topic_policy_default():
    mon = _make_monitor()
    assert mon.get_topic_policy("market_data") == "write"
    assert mon.get_topic_policy("unknown_table") == "write"


def test_topic_policy_from_env(monkeypatch):
    monkeypatch.setenv("HFT_WAL_FIRST_POLICY_LATENCY_SPANS", "drop")
    mon = _make_monitor()
    assert mon.get_topic_policy("latency_spans") == "drop"


def test_topic_policy_invalid_defaults_to_write(monkeypatch):
    monkeypatch.setenv("HFT_WAL_FIRST_POLICY_ORDERS", "invalid_value")
    mon = _make_monitor()
    assert mon.get_topic_policy("orders") == "write"
