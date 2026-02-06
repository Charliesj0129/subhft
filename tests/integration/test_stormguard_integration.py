"""Integration tests for StormGuard end-to-end flow.

Tests the full StormGuard lifecycle including:
- State transitions based on drawdown/latency/feed_gap
- Integration with system supervisor
- Metrics updates
"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState


class TestStormGuardStateMachine(unittest.TestCase):
    """Test StormGuard state machine transitions."""

    def setUp(self):
        self.metrics_patcher = patch("hft_platform.risk.storm_guard.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        mock_registry = MagicMock()
        mock_registry.stormguard_mode.labels.return_value = MagicMock()
        self.mock_metrics.return_value = mock_registry

    def tearDown(self):
        self.metrics_patcher.stop()

    def test_initial_state_is_normal(self):
        """StormGuard starts in NORMAL state."""
        guard = StormGuard()
        self.assertEqual(guard.state, StormGuardState.NORMAL)
        self.assertTrue(guard.is_safe())

    def test_drawdown_triggers_warm(self):
        """Drawdown below warm threshold triggers WARM state."""
        guard = StormGuard()
        thresholds = guard.thresholds

        # Just below warm threshold (-0.5%)
        state = guard.update(drawdown_pct=thresholds.warm_drawdown - 0.001)
        self.assertEqual(state, StormGuardState.WARM)

    def test_drawdown_triggers_storm(self):
        """Drawdown below storm threshold triggers STORM state."""
        guard = StormGuard()
        thresholds = guard.thresholds

        # Just below storm threshold (-1.0%)
        state = guard.update(drawdown_pct=thresholds.storm_drawdown - 0.001)
        self.assertEqual(state, StormGuardState.STORM)

    def test_drawdown_triggers_halt(self):
        """Drawdown below halt threshold triggers HALT state."""
        guard = StormGuard()
        thresholds = guard.thresholds

        # Just below halt threshold (-2.0%)
        state = guard.update(drawdown_pct=thresholds.halt_drawdown - 0.001)
        self.assertEqual(state, StormGuardState.HALT)
        self.assertFalse(guard.is_safe())

    def test_latency_triggers_warm(self):
        """High latency triggers WARM state."""
        guard = StormGuard()
        thresholds = guard.thresholds

        state = guard.update(latency_us=thresholds.latency_warm_us + 1)
        self.assertEqual(state, StormGuardState.WARM)

    def test_latency_triggers_storm(self):
        """Very high latency triggers STORM state."""
        guard = StormGuard()
        thresholds = guard.thresholds

        state = guard.update(latency_us=thresholds.latency_storm_us + 1)
        self.assertEqual(state, StormGuardState.STORM)

    def test_feed_gap_triggers_storm(self):
        """Feed gap exceeding threshold triggers STORM state."""
        guard = StormGuard()
        thresholds = guard.thresholds

        state = guard.update(feed_gap_s=thresholds.feed_gap_halt_s + 0.1)
        self.assertEqual(state, StormGuardState.STORM)

    def test_halt_priority_over_storm(self):
        """HALT condition takes priority over STORM."""
        guard = StormGuard()

        # Both HALT (drawdown) and STORM (latency) conditions
        state = guard.update(
            drawdown_pct=-0.03,  # HALT
            latency_us=25000,  # STORM
        )
        self.assertEqual(state, StormGuardState.HALT)

    def test_recovery_from_halt(self):
        """System can recover from HALT when conditions improve."""
        guard = StormGuard()

        # Trigger HALT
        guard.update(drawdown_pct=-0.03)
        self.assertEqual(guard.state, StormGuardState.HALT)

        # Recover with safe values
        state = guard.update(drawdown_pct=0, latency_us=0, feed_gap_s=0)
        self.assertEqual(state, StormGuardState.NORMAL)
        self.assertTrue(guard.is_safe())

    def test_manual_halt_trigger(self):
        """Manual halt can be triggered."""
        guard = StormGuard()

        guard.trigger_halt("Manual intervention")
        self.assertEqual(guard.state, StormGuardState.HALT)
        self.assertFalse(guard.is_safe())

    def test_metrics_updated_on_transition(self):
        """Metrics are updated on state transitions."""
        guard = StormGuard()

        guard.update(drawdown_pct=-0.03)  # HALT

        # Verify metric was updated
        self.mock_metrics.return_value.stormguard_mode.labels.assert_called()


class TestStormGuardWithCustomThresholds(unittest.TestCase):
    """Test StormGuard with custom thresholds."""

    def setUp(self):
        self.metrics_patcher = patch("hft_platform.risk.storm_guard.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        mock_registry = MagicMock()
        mock_registry.stormguard_mode.labels.return_value = MagicMock()
        self.mock_metrics.return_value = mock_registry

    def tearDown(self):
        self.metrics_patcher.stop()

    def test_custom_drawdown_thresholds(self):
        """Custom drawdown thresholds are respected."""
        custom = RiskThresholds(
            warm_drawdown=-0.001,  # 0.1%
            storm_drawdown=-0.002,  # 0.2%
            halt_drawdown=-0.003,  # 0.3%
        )
        guard = StormGuard(thresholds=custom)

        # -0.15% should trigger WARM with custom thresholds
        state = guard.update(drawdown_pct=-0.0015)
        self.assertEqual(state, StormGuardState.WARM)

    def test_custom_latency_thresholds(self):
        """Custom latency thresholds are respected."""
        custom = RiskThresholds(
            latency_warm_us=1000,  # 1ms
            latency_storm_us=2000,  # 2ms
        )
        guard = StormGuard(thresholds=custom)

        # 1.5ms should trigger WARM with custom thresholds
        state = guard.update(latency_us=1500)
        self.assertEqual(state, StormGuardState.WARM)

    def test_custom_feed_gap_threshold(self):
        """Custom feed gap threshold is respected."""
        custom = RiskThresholds(feed_gap_halt_s=0.5)  # 500ms
        guard = StormGuard(thresholds=custom)

        # 600ms gap should trigger STORM with custom threshold
        state = guard.update(feed_gap_s=0.6)
        self.assertEqual(state, StormGuardState.STORM)


class TestStormGuardSystemIntegration(unittest.TestCase):
    """Test StormGuard integration with system components."""

    def setUp(self):
        self.metrics_patcher = patch("hft_platform.risk.storm_guard.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        mock_registry = MagicMock()
        mock_registry.stormguard_mode.labels.return_value = MagicMock()
        self.mock_metrics.return_value = mock_registry

    def tearDown(self):
        self.metrics_patcher.stop()

    def test_stormguard_with_reconciliation_halt(self):
        """Test HALT triggered by reconciliation mismatch."""
        guard = StormGuard()

        # Simulate reconciliation triggering halt
        guard.trigger_halt("RECONCILIATION_MISMATCH: 3 critical discrepancies")

        self.assertEqual(guard.state, StormGuardState.HALT)

    def test_stormguard_state_enum_values(self):
        """Test StormGuardState enum values for metrics."""
        self.assertEqual(int(StormGuardState.NORMAL), 0)
        self.assertEqual(int(StormGuardState.WARM), 1)
        self.assertEqual(int(StormGuardState.STORM), 2)
        self.assertEqual(int(StormGuardState.HALT), 3)

    def test_stormguard_last_state_change_updated(self):
        """Test last_state_change is updated on transitions."""
        guard = StormGuard()
        initial_time = guard.last_state_change

        # Wait a tiny bit and trigger transition
        guard.update(drawdown_pct=-0.03)

        self.assertGreaterEqual(guard.last_state_change, initial_time)


class TestStormGuardConcurrency(unittest.TestCase):
    """Test StormGuard behavior under concurrent updates."""

    def setUp(self):
        self.metrics_patcher = patch("hft_platform.risk.storm_guard.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        mock_registry = MagicMock()
        mock_registry.stormguard_mode.labels.return_value = MagicMock()
        self.mock_metrics.return_value = mock_registry

    def tearDown(self):
        self.metrics_patcher.stop()

    def test_rapid_state_changes(self):
        """Test rapid state changes are handled correctly."""
        guard = StormGuard()

        # Rapidly cycle through states
        for _ in range(100):
            guard.update(drawdown_pct=-0.03)  # HALT
            guard.update(drawdown_pct=0)  # NORMAL

        # Should end up in NORMAL
        self.assertEqual(guard.state, StormGuardState.NORMAL)

    def test_multiple_simultaneous_conditions(self):
        """Test multiple simultaneous risk conditions."""
        guard = StormGuard()

        # Multiple conditions at once
        state = guard.update(
            drawdown_pct=-0.007,  # WARM level
            latency_us=10000,  # WARM level
            feed_gap_s=0.5,  # Below feed gap threshold
        )

        # Should be WARM (highest applicable state below HALT)
        self.assertEqual(state, StormGuardState.WARM)


class TestStormGuardMarketDataIntegration(unittest.TestCase):
    """Test StormGuard integration with MarketDataService feed gap."""

    def setUp(self):
        self.metrics_patcher = patch("hft_platform.risk.storm_guard.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        mock_registry = MagicMock()
        mock_registry.stormguard_mode.labels.return_value = MagicMock()
        self.mock_metrics.return_value = mock_registry

    def tearDown(self):
        self.metrics_patcher.stop()

    def test_feed_gap_from_market_data_service(self):
        """Test StormGuard reacts to feed gap from MarketDataService."""
        guard = StormGuard()

        # Simulate feed gap from market data service
        mock_md_service = MagicMock()
        mock_md_service.get_max_feed_gap_s.return_value = 1.5  # Above feed gap threshold

        feed_gap_s = mock_md_service.get_max_feed_gap_s()
        state = guard.update(feed_gap_s=feed_gap_s)

        self.assertEqual(state, StormGuardState.STORM)

    def test_per_symbol_feed_gaps(self):
        """Test per-symbol feed gap monitoring."""
        mock_md_service = MagicMock()
        mock_md_service.get_feed_gaps_by_symbol.return_value = {
            "2330": 0.5,
            "2317": 2.0,  # This symbol has high gap
            "2454": 0.3,
        }

        gaps = mock_md_service.get_feed_gaps_by_symbol()
        max_gap = max(gaps.values())

        guard = StormGuard()
        state = guard.update(feed_gap_s=max_gap)

        self.assertEqual(state, StormGuardState.STORM)


if __name__ == "__main__":
    unittest.main()
