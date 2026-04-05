"""Tests for FeatureEngine failure → StormGuard escalation (Task P2a)."""

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import StormGuardState


@pytest.fixture()
def mock_metrics():
    m = MagicMock()
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=m):
        yield m


@pytest.fixture()
def guard(mock_metrics):
    from hft_platform.risk.storm_guard import StormGuard

    return StormGuard()


class TestReportFeatureFailure:
    """report_feature_failure() escalates to STORM."""

    def test_escalates_to_storm_from_normal(self, guard):
        assert guard.state == StormGuardState.NORMAL
        guard.report_feature_failure(count=10)
        assert guard.state == StormGuardState.STORM

    def test_sets_feature_failure_active_flag(self, guard):
        guard.report_feature_failure(count=10)
        assert guard._feature_failure_active is True

    def test_does_not_downgrade_from_halt(self, guard):
        guard.trigger_halt("test")
        assert guard.state == StormGuardState.HALT
        guard.report_feature_failure(count=10)
        # Should remain HALT, not downgrade to STORM
        assert guard.state == StormGuardState.HALT
        # But flag should still be set
        assert guard._feature_failure_active is True

    def test_noop_when_already_storm(self, guard):
        guard.update(latency_us=25000)  # STORM via latency
        assert guard.state == StormGuardState.STORM
        guard.report_feature_failure(count=10)
        assert guard.state == StormGuardState.STORM
        assert guard._feature_failure_active is True

    def test_increments_metric(self, guard, mock_metrics):
        guard.report_feature_failure(count=10)
        mock_metrics.feature_engine_escalation_total.inc.assert_called_once()

    def test_multiple_calls_increment_metric_each_time(self, guard, mock_metrics):
        guard.report_feature_failure(count=10)
        guard.report_feature_failure(count=20)
        assert mock_metrics.feature_engine_escalation_total.inc.call_count == 2


class TestReportFeatureRecovery:
    """report_feature_recovery() clears feature-failure STORM."""

    def test_recovery_clears_flag_without_transitioning(self, guard):
        """report_feature_recovery() clears the flag but does NOT transition.
        The next update() cycle handles de-escalation."""
        guard.report_feature_failure(count=10)
        assert guard.state == StormGuardState.STORM
        # Bypass anti-flap hold time
        guard._feature_failure_storm_ts -= 10.0
        guard.report_feature_recovery()
        # Flag cleared, but state stays STORM until update() handles it
        assert guard._feature_failure_active is False
        assert guard.state == StormGuardState.STORM
        # update() with clear inputs de-escalates via hysteresis
        guard._storm_cooldown_s = 0.0
        guard._de_escalate_threshold = 1
        guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0)
        assert guard.state == StormGuardState.NORMAL

    def test_noop_when_no_feature_failure_active(self, guard):
        # Transition to STORM via other means
        guard.update(latency_us=25000)
        assert guard.state == StormGuardState.STORM
        guard.report_feature_recovery()
        # Should remain STORM since feature failure was not the cause
        assert guard.state == StormGuardState.STORM

    def test_noop_when_already_normal(self, guard):
        guard.report_feature_recovery()
        assert guard.state == StormGuardState.NORMAL

    def test_does_not_downgrade_from_halt(self, guard):
        guard.report_feature_failure(count=10)
        guard.trigger_halt("critical")
        assert guard.state == StormGuardState.HALT
        # Bypass anti-flap hold time
        guard._feature_failure_storm_ts -= 10.0
        guard.report_feature_recovery()
        # HALT must persist — recovery only clears STORM from feature failure
        assert guard.state == StormGuardState.HALT
        # But feature flag should be cleared
        assert guard._feature_failure_active is False


    def test_dual_cause_storm_recovery_preserves_other_cause(self, guard):
        """If latency AND feature failure both cause STORM, feature recovery
        clears the flag but state remains STORM (latency still active)."""
        # Latency causes STORM first
        guard.update(latency_us=25000)
        assert guard.state == StormGuardState.STORM

        # Feature failure also fires (sets flag, but state already STORM)
        guard.report_feature_failure(count=10)
        assert guard._feature_failure_active is True
        assert guard.state == StormGuardState.STORM

        # Bypass the anti-flap hold time
        guard._feature_failure_storm_ts -= 10.0

        # Feature recovers — but latency is still elevated
        guard.report_feature_recovery()
        assert guard._feature_failure_active is False
        # State MUST remain STORM — latency condition is still active
        assert guard.state == StormGuardState.STORM

    def test_feature_failure_prevents_deescalation_to_warm(self, guard):
        """_feature_failure_active must block de-escalation even when drawdown/latency
        are only in the WARM range (not STORM range).

        Regression: previously _feature_failure_active was checked AFTER the WARM
        threshold returns, so update() with warm-range inputs would return WARM
        and the hysteresis loop would de-escalate from STORM→WARM while
        FeatureEngine was still broken.
        """
        guard._storm_cooldown_s = 0.0
        guard._de_escalate_threshold = 1

        guard.report_feature_failure(count=10)
        assert guard.state == StormGuardState.STORM

        # drawdown_bps=-60 is in the WARM range (-50 threshold), latency clean
        result = guard.update(drawdown_bps=-60, latency_us=0, feed_gap_s=0.0)
        # Must remain STORM — feature failure is still active
        assert result == StormGuardState.STORM
        assert guard.state == StormGuardState.STORM

    def test_feature_failure_cleared_allows_deescalation_from_warm_range(self, guard):
        """After feature recovery, update() with WARM-range inputs de-escalates to WARM."""
        guard._storm_cooldown_s = 0.0
        guard._de_escalate_threshold = 1

        guard.report_feature_failure(count=10)
        guard._feature_failure_storm_ts -= 10.0  # bypass anti-flap
        guard.report_feature_recovery()
        assert guard._feature_failure_active is False

        # Now drawdown_bps=-60 is WARM range — should de-escalate to WARM
        result = guard.update(drawdown_bps=-60, latency_us=0, feed_gap_s=0.0)
        assert result == StormGuardState.WARM
        assert guard.state == StormGuardState.WARM

    def test_recovery_suppressed_during_hold_period(self, guard):
        """Anti-flap: recovery within _FEATURE_RECOVERY_HOLD_S is suppressed."""
        guard.report_feature_failure(count=10)
        assert guard.state == StormGuardState.STORM

        # Immediate recovery attempt — should be suppressed
        guard.report_feature_recovery()
        assert guard.state == StormGuardState.STORM
        assert guard._feature_failure_active is True  # flag NOT cleared

        # Advance past hold time
        guard._feature_failure_storm_ts -= 10.0

        # Now recovery clears the flag (but does not transition)
        guard.report_feature_recovery()
        assert guard._feature_failure_active is False
        # State stays STORM — update() handles de-escalation
        assert guard.state == StormGuardState.STORM


class TestMarketDataServiceFeatureEscalation:
    """Integration: _maybe_update_features calls storm_guard on failure/recovery."""

    def _make_mds(self, feature_engine, storm_guard):
        """Build a minimal MarketDataService with mocked dependencies."""
        import asyncio
        from unittest.mock import MagicMock

        from hft_platform.services.market_data import MarketDataService

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        bus.publish_many_nowait = MagicMock()
        raw_queue = asyncio.Queue(maxsize=100)
        client = MagicMock()

        with patch("hft_platform.services.market_data.MetricsRegistry.get", return_value=MagicMock()):
            mds = MarketDataService(
                bus=bus,
                raw_queue=raw_queue,
                client=client,
                feature_engine=feature_engine,
                storm_guard=storm_guard,
            )
        return mds

    def test_consecutive_failures_trigger_storm_guard(self, guard, mock_metrics):
        """After _FEATURE_FAILURE_ESCALATE consecutive failures, storm_guard.report_feature_failure is called."""
        fe = MagicMock()
        fe.feature_set_id.return_value = "test_v1"
        fe.process_lob_stats = MagicMock(side_effect=RuntimeError("boom"))
        fe.process_lob_update = None

        mds = self._make_mds(fe, guard)
        mds._fe_process_lob_update = None  # Force fallback to process_lob_stats

        # Build a minimal BidAskEvent-like object
        event = MagicMock()
        event.symbol = "2330"
        event.meta = MagicMock()
        event.meta.local_ts = 1000
        event.trade_direction = 0

        # stats must be a tuple to pass the isinstance guard in _maybe_update_features
        stats = (100000, 500, 0.5)

        # Fire N failures (below threshold) — should NOT escalate
        for _ in range(mds._FEATURE_FAILURE_ESCALATE - 1):
            mds._maybe_update_features(event, stats)

        assert guard.state == StormGuardState.NORMAL

        # One more failure pushes past threshold — STORM
        mds._maybe_update_features(event, stats)
        assert guard.state == StormGuardState.STORM

    def test_recovery_after_failure_clears_storm(self, guard, mock_metrics):
        """A successful feature computation after failures calls report_feature_recovery."""
        call_count = [0]

        def process_lob_stats_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 11:
                raise RuntimeError("boom")
            return MagicMock()

        fe = MagicMock()
        fe.feature_set_id.return_value = "test_v1"
        fe.process_lob_stats = MagicMock(side_effect=process_lob_stats_side_effect)
        fe.process_lob_update = None

        mds = self._make_mds(fe, guard)
        mds._fe_process_lob_update = None
        mds._FEATURE_FAILURE_ESCALATE = 10

        event = MagicMock()
        event.symbol = "2330"
        event.meta = MagicMock()
        event.meta.local_ts = 1000
        event.trade_direction = 0

        # stats must be a tuple to pass the isinstance guard
        stats = (100000, 500, 0.5)

        # 11 failures (exceeds threshold of 10)
        for _ in range(11):
            mds._maybe_update_features(event, stats)
        assert guard.state == StormGuardState.STORM

        # Bypass anti-flap hold time so recovery can proceed
        guard._feature_failure_storm_ts -= 10.0

        # Next call succeeds → flag cleared (state stays STORM until update())
        mds._maybe_update_features(event, stats)
        assert guard._feature_failure_active is False
        assert guard.state == StormGuardState.STORM
        # update() with clear inputs de-escalates via hysteresis
        guard._storm_cooldown_s = 0.0
        guard._de_escalate_threshold = 1
        guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0)
        assert guard.state == StormGuardState.NORMAL
