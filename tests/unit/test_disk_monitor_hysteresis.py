"""DiskPressureMonitor hysteresis band prevents 1-byte threshold thrash."""

from __future__ import annotations

from hft_platform.recorder.disk_monitor import DiskPressureLevel, DiskPressureMonitor


def _make_monitor(hysteresis: float = 0.10) -> DiskPressureMonitor:
    mon = DiskPressureMonitor(warn_mb=100.0, critical_mb=300.0, halt_mb=500.0)
    mon._hysteresis_fraction = hysteresis
    return mon


def test_escalation_is_instant_at_threshold():
    mon = _make_monitor()
    # OK → WARN at exactly the threshold
    assert mon._compute_level(100.0, DiskPressureLevel.OK) == DiskPressureLevel.WARN
    # WARN → CRITICAL
    assert mon._compute_level(300.0, DiskPressureLevel.WARN) == DiskPressureLevel.CRITICAL
    # CRITICAL → HALT
    assert mon._compute_level(500.0, DiskPressureLevel.CRITICAL) == DiskPressureLevel.HALT


def test_warn_sticky_within_hysteresis_band():
    mon = _make_monitor(hysteresis=0.10)
    # In WARN, size drops to 95 (5% below 100) → stays WARN (exit = 90)
    assert mon._compute_level(95.0, DiskPressureLevel.WARN) == DiskPressureLevel.WARN
    # drops to 89.9 (just below 90 exit) → OK
    assert mon._compute_level(89.9, DiskPressureLevel.WARN) == DiskPressureLevel.OK


def test_critical_sticky_within_hysteresis_band():
    mon = _make_monitor(hysteresis=0.10)
    # In CRITICAL (enter at 300), size drops to 280 (exit = 270) → stays CRITICAL
    assert mon._compute_level(280.0, DiskPressureLevel.CRITICAL) == DiskPressureLevel.CRITICAL
    # drops to 269 → WARN
    assert mon._compute_level(269.0, DiskPressureLevel.CRITICAL) == DiskPressureLevel.WARN


def test_halt_sticky_within_hysteresis_band():
    mon = _make_monitor(hysteresis=0.10)
    # In HALT (enter at 500), size drops to 460 (exit = 450) → stays HALT
    assert mon._compute_level(460.0, DiskPressureLevel.HALT) == DiskPressureLevel.HALT
    # drops to 449 → CRITICAL (still above 300)
    assert mon._compute_level(449.0, DiskPressureLevel.HALT) == DiskPressureLevel.CRITICAL


def test_disabling_hysteresis_reverts_to_sharp_threshold():
    mon = _make_monitor(hysteresis=0.0)
    # At exactly 99 (below warn threshold) → OK regardless of current level
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
    # Without hysteresis this would oscillate 4 times. With 10% band, at most 1 transition.
    assert transitions <= 1, f"hysteresis should dampen thrash, got {transitions} transitions"
