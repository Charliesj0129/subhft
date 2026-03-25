"""Tests for AutonomyMonitor: HALT reaction, normal no decisions, cooldown, broker disconnect."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.ops.autonomy_monitor import AutonomyMonitor, MonitorDecision


def _make_monitor(**overrides) -> AutonomyMonitor:
    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL
    platform_degrade = MagicMock()
    platform_degrade.reduce_only_active = False
    platform_inputs = MagicMock()
    platform_inputs.reduce_only_reasons = MagicMock(return_value=[])

    kwargs = dict(
        storm_guard=storm_guard,
        platform_degrade=platform_degrade,
        platform_inputs=platform_inputs,
    )
    kwargs.update(overrides)
    return AutonomyMonitor(**kwargs)


class TestHaltReaction:
    def test_halt_produces_flatten_decision(self) -> None:
        monitor = _make_monitor()
        monitor._storm_guard.state = StormGuardState.HALT
        decisions = monitor._evaluate()
        assert len(decisions) == 1
        assert decisions[0].action == "flatten_all"
        assert decisions[0].reason == "stormguard_halt"

    def test_halt_only_reacts_once(self) -> None:
        monitor = _make_monitor()
        monitor._storm_guard.state = StormGuardState.HALT
        decisions1 = monitor._evaluate()
        assert len(decisions1) == 1
        # Simulate that the execution set _halt_reacted
        monitor._halt_reacted = True
        decisions2 = monitor._evaluate()
        assert len(decisions2) == 0


class TestNormalNoDecisions:
    def test_normal_state_no_decisions(self) -> None:
        monitor = _make_monitor()
        decisions = monitor._evaluate()
        assert decisions == []

    def test_warm_state_no_decisions(self) -> None:
        monitor = _make_monitor()
        monitor._storm_guard.state = StormGuardState.WARM
        decisions = monitor._evaluate()
        assert decisions == []


class TestCooldown:
    def test_cooldown_prevents_duplicate_decisions(self) -> None:
        monitor = _make_monitor()
        monitor._platform_inputs.reduce_only_reasons.return_value = ["rss_unhealthy"]
        decisions1 = monitor._evaluate()
        assert len(decisions1) == 1
        # Apply cooldowns
        monitor._apply_cooldowns(decisions1)
        # Second evaluation should be blocked by cooldown
        decisions2 = monitor._evaluate()
        assert len(decisions2) == 0


class TestBrokerDisconnect:
    def test_broker_disconnect_over_threshold_triggers_reduce_only(self) -> None:
        broker = MagicMock()
        broker.is_connected = MagicMock(return_value=False)
        monitor = _make_monitor(broker_client=broker)

        # Simulate disconnect detected at first check
        monitor._broker_was_connected = False
        monitor._broker_disconnect_since_ns = 0  # long time ago

        decisions: list[MonitorDecision] = []
        now_ns = 999_999_999_999  # large enough to exceed 300s threshold
        monitor._check_broker_disconnect(decisions, now_ns)
        assert len(decisions) == 1
        assert decisions[0].reason == "broker_unavailable"

    def test_connected_broker_no_decisions(self) -> None:
        broker = MagicMock()
        broker.is_connected = MagicMock(return_value=True)
        monitor = _make_monitor(broker_client=broker)
        decisions: list[MonitorDecision] = []
        monitor._check_broker_disconnect(decisions, 1_000_000_000)
        assert decisions == []
