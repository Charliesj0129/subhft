import pytest
@pytest.mark.chaos
class TestStormGuardHalt:
    def test_halt_blocks_orders(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState, RiskThresholds
        guard = StormGuard(thresholds=RiskThresholds(halt_drawdown_bps=-200))
        guard.trigger_halt("Test")
        assert guard.state == StormGuardState.HALT

    def test_drawdown_triggers_halt(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState, RiskThresholds
        guard = StormGuard(thresholds=RiskThresholds(halt_drawdown_bps=-200))
        guard.update(drawdown_bps=-300, latency_us=100, feed_gap_s=0.0)
        assert guard.state == StormGuardState.HALT
