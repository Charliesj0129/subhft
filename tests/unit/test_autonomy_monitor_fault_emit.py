"""Tests for AutonomyMonitor emitting FaultEvents when HFT_HEALING_ENABLED=1."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import StormGuardState


@pytest.fixture
def mock_deps():
    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL
    platform_degrade = MagicMock()
    platform_degrade.reduce_only_active = False
    platform_inputs = MagicMock()
    platform_inputs.reduce_only_reasons.return_value = []
    return storm_guard, platform_degrade, platform_inputs


class TestAutonomyMonitorFaultEmit:
    @patch.dict("os.environ", {"HFT_HEALING_ENABLED": "1"})
    def test_broker_disconnect_emits_fault_event_when_healing_enabled(self, mock_deps):
        storm_guard, platform_degrade, platform_inputs = mock_deps
        from hft_platform.ops.autonomy_monitor import AutonomyMonitor

        fault_callback = MagicMock()
        broker = MagicMock()
        broker.is_connected.return_value = False

        monitor = AutonomyMonitor(
            storm_guard=storm_guard,
            platform_degrade=platform_degrade,
            platform_inputs=platform_inputs,
            broker_client=broker,
            fault_callback=fault_callback,
        )
        monitor._broker_was_connected = False
        # Set disconnect time to >300s ago
        from hft_platform.core import timebase
        now = timebase.now_ns()
        monitor._broker_disconnect_since_ns = now - 301_000_000_000

        decisions = monitor._evaluate()

        fault_callback.assert_called_once()
        fault = fault_callback.call_args.args[0]
        from hft_platform.healing.fault import FaultCategory
        assert fault.category == FaultCategory.BROKER
        assert "disconnect" in fault.description
        # Should NOT have appended a MonitorDecision
        assert len(decisions) == 0

    def test_broker_disconnect_uses_legacy_when_healing_disabled(self, mock_deps):
        storm_guard, platform_degrade, platform_inputs = mock_deps
        from hft_platform.ops.autonomy_monitor import AutonomyMonitor

        broker = MagicMock()
        broker.is_connected.return_value = False

        monitor = AutonomyMonitor(
            storm_guard=storm_guard,
            platform_degrade=platform_degrade,
            platform_inputs=platform_inputs,
            broker_client=broker,
        )
        monitor._broker_was_connected = False
        from hft_platform.core import timebase
        now = timebase.now_ns()
        monitor._broker_disconnect_since_ns = now - 301_000_000_000

        decisions = monitor._evaluate()
        # Legacy path: should have a MonitorDecision
        assert len(decisions) >= 1
        assert decisions[0].action == "enter_reduce_only"

    def test_fault_callback_is_optional(self, mock_deps):
        """AutonomyMonitor works without fault_callback (backward compat)."""
        storm_guard, platform_degrade, platform_inputs = mock_deps
        from hft_platform.ops.autonomy_monitor import AutonomyMonitor

        monitor = AutonomyMonitor(
            storm_guard=storm_guard,
            platform_degrade=platform_degrade,
            platform_inputs=platform_inputs,
        )
        # Should not raise
        decisions = monitor._evaluate()
        assert isinstance(decisions, list)
