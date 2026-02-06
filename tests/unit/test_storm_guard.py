import unittest
from unittest.mock import MagicMock, patch

from hft_platform.risk.storm_guard import StormGuard, StormGuardState


class TestStormGuard(unittest.TestCase):
    def setUp(self):
        self.temp_metrics = MagicMock()
        # Mock MetricsRegistry.get() to avoid side effects
        patcher = patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=self.temp_metrics)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.guard = StormGuard()

    def test_initial_state(self):
        self.assertEqual(self.guard.state, StormGuardState.NORMAL)

    def test_transition_to_warm(self):
        # Drawdown -0.0051 (< -0.005) -> WARM
        state = self.guard.update(drawdown_pct=-0.006)
        self.assertEqual(state, StormGuardState.WARM)
        self.assertEqual(self.guard.state, StormGuardState.WARM)

    def test_transition_to_storm(self):
        # Latency 21000 (> 20000) -> STORM
        state = self.guard.update(latency_us=21000)
        self.assertEqual(state, StormGuardState.STORM)

    def test_transition_to_halt(self):
        # Feed Gap 1.1 (> 1.0) -> STORM (should not HALT)
        state = self.guard.update(feed_gap_s=1.1)
        self.assertEqual(state, StormGuardState.STORM)

    def test_priority(self):
        # Halt condition AND Storm condition -> Should be HALT
        state = self.guard.update(drawdown_pct=-0.10, latency_us=20000)
        self.assertEqual(state, StormGuardState.HALT)

    def test_manual_halt(self):
        self.guard.trigger_halt("Manual")
        self.assertEqual(self.guard.state, StormGuardState.HALT)
        self.assertFalse(self.guard.is_safe())

    def test_recovery(self):
        self.guard.trigger_halt("Manual")
        # Update with safe values
        state = self.guard.update(drawdown_pct=0, latency_us=0, feed_gap_s=0)
        self.assertEqual(state, StormGuardState.NORMAL)
        self.assertTrue(self.guard.is_safe())
