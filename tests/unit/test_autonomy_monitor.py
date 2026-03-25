"""Tests for AutonomyMonitor."""

from __future__ import annotations

from hft_platform.ops.autonomy import AutonomyMode
from hft_platform.ops.autonomy_monitor import AutonomyMonitor, HealthSnapshot, MonitorConfig


def _default_snap(**overrides) -> HealthSnapshot:
    defaults = {
        "feed_gap_s": 0.0,
        "memory_rss_gb": 1.0,
        "memory_limit_gb": 4.0,
        "persistence_healthy": True,
        "broker_connected": True,
        "reconciliation_drift_streak": 0,
        "storm_guard_state": "NORMAL",
    }
    defaults.update(overrides)
    return HealthSnapshot(**defaults)


class TestAutonomyMonitorEvaluation:
    def test_healthy_snapshot_stays_normal(self) -> None:
        mon = AutonomyMonitor()
        result = mon.evaluate(_default_snap())
        assert result == AutonomyMode.NORMAL

    def test_feed_gap_triggers_reduce_only(self) -> None:
        config = MonitorConfig(feed_gap_degrade_s=5.0, feed_gap_halt_s=30.0)
        mon = AutonomyMonitor(config=config)
        result = mon.evaluate(_default_snap(feed_gap_s=10.0))
        assert result == AutonomyMode.PLATFORM_REDUCE_ONLY

    def test_feed_gap_triggers_halt(self) -> None:
        config = MonitorConfig(feed_gap_halt_s=30.0)
        mon = AutonomyMonitor(config=config)
        result = mon.evaluate(_default_snap(feed_gap_s=35.0))
        assert result == AutonomyMode.HALT

    def test_broker_disconnect_triggers_reduce_only(self) -> None:
        mon = AutonomyMonitor()
        result = mon.evaluate(_default_snap(broker_connected=False))
        assert result == AutonomyMode.PLATFORM_REDUCE_ONLY

    def test_persistence_failure_triggers_reduce_only(self) -> None:
        mon = AutonomyMonitor()
        result = mon.evaluate(_default_snap(persistence_healthy=False))
        assert result == AutonomyMode.PLATFORM_REDUCE_ONLY

    def test_memory_pressure_triggers_halt(self) -> None:
        config = MonitorConfig(memory_halt_pct=0.95)
        mon = AutonomyMonitor(config=config)
        # 3.9 GB out of 4.0 GB = 97.5% > 95%
        result = mon.evaluate(_default_snap(memory_rss_gb=3.9, memory_limit_gb=4.0))
        assert result == AutonomyMode.HALT


class TestAutonomyMonitorRearm:
    def test_rearm_resets_to_normal(self) -> None:
        mon = AutonomyMonitor()
        mon.evaluate(_default_snap(broker_connected=False))
        assert mon.mode == AutonomyMode.PLATFORM_REDUCE_ONLY
        mon.rearm()
        assert mon.mode == AutonomyMode.NORMAL


class TestAutonomyMonitorTransitionCallback:
    def test_on_transition_callback_called(self) -> None:
        transitions: list = []
        mon = AutonomyMonitor(on_transition=lambda t: transitions.append(t))
        mon.evaluate(_default_snap(broker_connected=False))
        assert len(transitions) == 1
        assert transitions[0].to_mode == AutonomyMode.PLATFORM_REDUCE_ONLY

    def test_transition_count_increments(self) -> None:
        mon = AutonomyMonitor()
        assert mon.transition_count == 0
        mon.evaluate(_default_snap(broker_connected=False))
        assert mon.transition_count == 1
