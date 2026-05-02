"""Tests for AutonomyMonitor: HALT reaction, normal no decisions, cooldown, broker disconnect."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_reduce_only_still_checks_broker_disconnect(self) -> None:
        """M15: broker disconnect check runs even during reduce_only."""
        broker = MagicMock()
        broker.is_connected = MagicMock(return_value=False)
        monitor = _make_monitor(broker_client=broker)
        monitor._platform_degrade.reduce_only_active = True
        # Already disconnected since t=0; now_ns is well past the 300s threshold
        monitor._broker_was_connected = False
        monitor._broker_disconnect_since_ns = 0

        now_ns = 999_999_999_999  # >> 300_000_000_000 threshold
        with patch("hft_platform.ops.autonomy_monitor.timebase") as mock_tb:
            mock_tb.now_ns.return_value = now_ns
            decisions = monitor._evaluate()

        broker_decisions = [d for d in decisions if d.reason == "broker_unavailable"]
        assert len(broker_decisions) >= 1


class TestHaltFlattenRetry:
    """Tests for C2: HALT flatten retry cap and H2: cooldown-guarded reset."""

    @pytest.mark.asyncio
    async def test_halt_flatten_failure_retries_up_to_max(self) -> None:
        flattener = AsyncMock()
        flattener.flatten_all.side_effect = RuntimeError("broker down")
        monitor = _make_monitor(position_flattener=flattener)
        monitor._storm_guard.state = StormGuardState.HALT

        # Advance time by 1 hour between evaluations so backoff gate never blocks
        base_ns = 1_000_000_000_000
        tick_ns = 3_600_000_000_000  # 1 hour in ns
        call_count = 0
        with patch("hft_platform.ops.autonomy_monitor.timebase") as mock_tb:
            mock_tb.now_ns.return_value = base_ns
            for i in range(5):
                mock_tb.now_ns.return_value = base_ns + i * tick_ns
                decisions = monitor._evaluate()
                if decisions:
                    await monitor._execute(decisions)
                    call_count += 1

        # Should have attempted exactly 3 times (max retries), then stopped
        assert flattener.flatten_all.call_count == 3
        assert call_count == 3
        assert monitor._halt_flatten_attempts == 3

    @pytest.mark.asyncio
    async def test_halt_flatten_failure_sets_reacted_after_max_retries(self) -> None:
        flattener = AsyncMock()
        flattener.flatten_all.side_effect = RuntimeError("broker down")
        monitor = _make_monitor(position_flattener=flattener)
        monitor._storm_guard.state = StormGuardState.HALT

        base_ns = 1_000_000_000_000
        tick_ns = 3_600_000_000_000  # 1 hour in ns
        with patch("hft_platform.ops.autonomy_monitor.timebase") as mock_tb:
            for i in range(3):
                mock_tb.now_ns.return_value = base_ns + i * tick_ns
                decisions = monitor._evaluate()
                if decisions:
                    await monitor._execute(decisions)

        assert monitor._halt_reacted is True
        assert monitor._halt_reacted_ns > 0

    @patch("hft_platform.ops.autonomy_monitor.timebase")
    def test_halt_reacted_reset_requires_cooldown(self, mock_tb: MagicMock) -> None:
        monitor = _make_monitor()
        # Simulate a completed HALT reaction at t=100s
        monitor._halt_reacted = True
        monitor._halt_reacted_ns = 100_000_000_000

        # At t=130s (30s later), switch to NORMAL — should NOT reset
        mock_tb.now_ns.return_value = 130_000_000_000
        monitor._storm_guard.state = StormGuardState.NORMAL
        monitor._evaluate()
        assert monitor._halt_reacted is True

        # At t=161s (61s after reacted), switch to NORMAL — should reset
        mock_tb.now_ns.return_value = 161_000_000_000
        monitor._evaluate()
        assert monitor._halt_reacted is False
        assert monitor._halt_flatten_attempts == 0

    @pytest.mark.asyncio
    @patch("hft_platform.ops.autonomy_monitor.timebase")
    async def test_halt_flatten_backoff_between_retries(self, mock_tb: MagicMock) -> None:
        """Retries use exponential backoff: 5s, 10s, 20s."""
        flattener = MagicMock()
        flattener.flatten_all = AsyncMock(side_effect=RuntimeError("broker down"))
        monitor = _make_monitor(position_flattener=flattener)
        monitor._storm_guard.state = StormGuardState.HALT

        current_time = 1_000_000_000_000  # 1000s in ns

        # Attempt 1: immediate (no backoff yet, _halt_next_retry_ns == 0)
        mock_tb.now_ns.return_value = current_time
        decisions = monitor._evaluate()
        assert len(decisions) == 1
        await monitor._execute(decisions)
        assert flattener.flatten_all.call_count == 1
        # _halt_next_retry_ns should be set to current + 5s
        assert monitor._halt_next_retry_ns > current_time

        # Too early for retry (only 3s later)
        current_time += 3_000_000_000
        mock_tb.now_ns.return_value = current_time
        decisions = monitor._evaluate()
        assert len(decisions) == 0  # blocked by backoff

        # After 5s total, retry allowed (6s since attempt 1)
        current_time += 3_000_000_000  # total 6s from attempt 1
        mock_tb.now_ns.return_value = current_time
        decisions = monitor._evaluate()
        assert len(decisions) == 1
        await monitor._execute(decisions)
        assert flattener.flatten_all.call_count == 2

    @patch("hft_platform.ops.autonomy_monitor.timebase")
    def test_halt_oscillation_no_double_flatten(self, mock_tb: MagicMock) -> None:
        monitor = _make_monitor(position_flattener=AsyncMock())
        # First HALT at t=100s
        mock_tb.now_ns.return_value = 100_000_000_000
        monitor._storm_guard.state = StormGuardState.HALT
        decisions1 = monitor._evaluate()
        assert len(decisions1) == 1
        assert decisions1[0].action == "flatten_all"

        # Simulate successful flatten
        monitor._halt_reacted = True
        monitor._halt_reacted_ns = 100_000_000_000

        # Oscillate to WARM at t=110s (only 10s later)
        mock_tb.now_ns.return_value = 110_000_000_000
        monitor._storm_guard.state = StormGuardState.WARM
        monitor._evaluate()
        # Should NOT have reset due to cooldown
        assert monitor._halt_reacted is True

        # Back to HALT at t=120s — should NOT produce flatten decision
        mock_tb.now_ns.return_value = 120_000_000_000
        monitor._storm_guard.state = StormGuardState.HALT
        decisions2 = monitor._evaluate()
        assert len(decisions2) == 0
