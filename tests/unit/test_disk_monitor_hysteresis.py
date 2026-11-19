"""DiskPressureMonitor hysteresis band prevents 1-byte threshold thrash."""

from __future__ import annotations

from hft_platform.recorder.disk_monitor import DiskPressureLevel, DiskPressureMonitor


def _make_monitor(hysteresis: float = 0.10) -> DiskPressureMonitor:
    mon = DiskPressureMonitor(warn_mb=100.0, critical_mb=300.0, halt_mb=500.0)
    mon._hysteresis_fraction = hysteresis
    return mon


def test_escalation_is_instant_at_threshold():
    mon = _make_monitor()
    assert mon._compute_level(100.0, DiskPressureLevel.OK) == DiskPressureLevel.WARN
    assert mon._compute_level(300.0, DiskPressureLevel.WARN) == DiskPressureLevel.CRITICAL
    assert mon._compute_level(500.0, DiskPressureLevel.CRITICAL) == DiskPressureLevel.HALT


def test_warn_sticky_within_hysteresis_band():
    mon = _make_monitor(hysteresis=0.10)
    assert mon._compute_level(95.0, DiskPressureLevel.WARN) == DiskPressureLevel.WARN
    assert mon._compute_level(89.9, DiskPressureLevel.WARN) == DiskPressureLevel.OK


def test_critical_sticky_within_hysteresis_band():
    mon = _make_monitor(hysteresis=0.10)
    assert mon._compute_level(280.0, DiskPressureLevel.CRITICAL) == DiskPressureLevel.CRITICAL
    assert mon._compute_level(269.0, DiskPressureLevel.CRITICAL) == DiskPressureLevel.WARN


def test_halt_sticky_within_hysteresis_band():
    mon = _make_monitor(hysteresis=0.10)
    assert mon._compute_level(460.0, DiskPressureLevel.HALT) == DiskPressureLevel.HALT
    assert mon._compute_level(449.0, DiskPressureLevel.HALT) == DiskPressureLevel.CRITICAL


def test_disabling_hysteresis_reverts_to_sharp_threshold():
    mon = _make_monitor(hysteresis=0.0)
    assert mon._compute_level(99.0, DiskPressureLevel.WARN) == DiskPressureLevel.OK


def test_oscillation_near_threshold_holds_state():
    """Simulate the observed production thrash: size bouncing 99.5 ↔ 100.5."""
    mon = _make_monitor(hysteresis=0.10)
    level = DiskPressureLevel.OK
    transitions = 0
    for size in [99.5, 100.5, 99.5, 100.5, 99.5]:
        new = mon._compute_level(size, level)
        if new != level:
            transitions += 1
            level = new
    assert transitions <= 1, f"hysteresis should dampen thrash, got {transitions} transitions"
