import pytest


@pytest.mark.chaos
class TestHalt:
    def test_halt(self):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState

        g = StormGuard(thresholds=RiskThresholds())
        g.trigger_halt("Test")
        assert g.state == StormGuardState.HALT

    def test_health_inputs_never_halt(self):
        from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState

        g = StormGuard(thresholds=RiskThresholds())
        g.update(latency_us=10_000_000, order_rtt_us=60_000_000, feed_gap_s=3600.0)
        assert g.state == StormGuardState.STORM
