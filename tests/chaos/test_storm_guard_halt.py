import pytest


@pytest.mark.chaos
class TestHalt:
    def test_halt(self):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState

        g = StormGuard(thresholds=RiskThresholds(halt_drawdown_bps=-200))
        g.trigger_halt("Test")
        assert g.state == StormGuardState.HALT

    def test_drawdown_halt(self):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState

        g = StormGuard(thresholds=RiskThresholds(halt_drawdown_bps=-200))
        g.update(drawdown_bps=-300, latency_us=100, feed_gap_s=0.0)
        assert g.state == StormGuardState.HALT
