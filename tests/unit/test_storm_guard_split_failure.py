"""Tests for split normalizer/feature failure flags in StormGuard.

Verifies that normalizer and FeatureEngine failures are tracked independently,
preventing premature de-escalation when only one domain recovers.
"""

import time

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.risk.storm_guard import RiskThresholds, StormGuard


@pytest.fixture()
def sg() -> StormGuard:
    """StormGuard with short cooldowns for test speed."""
    sg = StormGuard(thresholds=RiskThresholds())
    sg._storm_cooldown_s = 0.0
    sg._halt_cooldown_s = 0.0
    sg._de_escalate_threshold = 1
    return sg


class TestSplitFailureFlags:
    """Verify independent tracking of normalizer vs feature failure domains."""

    def test_norm_failure_active_feature_recovery_stays_storm(self, sg: StormGuard) -> None:
        """When norm failure is active, feature recovery must NOT clear STORM."""
        sg.report_norm_failure(5)
        assert sg.state == StormGuardState.STORM

        sg.report_feature_failure(3)
        assert sg.state == StormGuardState.STORM

        # Bypass anti-flap hold for feature recovery
        sg._feature_failure_storm_ts = time.monotonic() - 10.0
        sg.report_feature_recovery()

        # Feature flag cleared, but norm flag still active
        assert not sg._feature_failure_active
        assert sg._norm_failure_active

        # update() should keep STORM due to norm failure
        result = sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
        assert result == StormGuardState.STORM

    def test_feature_failure_active_norm_recovery_stays_storm(self, sg: StormGuard) -> None:
        """When feature failure is active, norm recovery must NOT clear STORM."""
        sg.report_feature_failure(5)
        assert sg.state == StormGuardState.STORM

        sg.report_norm_failure(3)
        assert sg.state == StormGuardState.STORM

        # Bypass anti-flap hold for norm recovery
        sg._norm_failure_storm_ts = time.monotonic() - 10.0
        sg.report_norm_recovery()

        # Norm flag cleared, but feature flag still active
        assert not sg._norm_failure_active
        assert sg._feature_failure_active

        result = sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
        assert result == StormGuardState.STORM

    def test_both_active_both_recover_clears_storm(self, sg: StormGuard) -> None:
        """When both failures recover (after anti-flap), STORM should clear."""
        sg.report_norm_failure(5)
        sg.report_feature_failure(3)
        assert sg.state == StormGuardState.STORM
        assert sg._norm_failure_active
        assert sg._feature_failure_active

        # Bypass anti-flap for both
        past = time.monotonic() - 10.0
        sg._norm_failure_storm_ts = past
        sg._feature_failure_storm_ts = past

        sg.report_norm_recovery()
        sg.report_feature_recovery()

        assert not sg._norm_failure_active
        assert not sg._feature_failure_active

        # With no other STORM conditions, update should de-escalate
        result = sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
        assert result == StormGuardState.NORMAL

    def test_norm_failure_alone_escalates_to_storm(self, sg: StormGuard) -> None:
        """Normalizer failure alone should escalate to STORM."""
        assert sg.state == StormGuardState.NORMAL

        sg.report_norm_failure(5)

        assert sg.state == StormGuardState.STORM
        assert sg._norm_failure_active
        assert not sg._feature_failure_active

    def test_norm_recovery_alone_clears_norm_flag(self, sg: StormGuard) -> None:
        """Normalizer recovery should only clear the norm flag."""
        sg.report_norm_failure(5)
        assert sg._norm_failure_active

        # Bypass anti-flap hold
        sg._norm_failure_storm_ts = time.monotonic() - 10.0
        sg.report_norm_recovery()

        assert not sg._norm_failure_active
        # Feature flag unchanged
        assert not sg._feature_failure_active

    def test_norm_recovery_anti_flap_suppressed(self, sg: StormGuard) -> None:
        """Norm recovery within anti-flap hold period should be suppressed."""
        sg.report_norm_failure(5)
        assert sg._norm_failure_active

        # Do NOT bypass anti-flap -- recovery should be suppressed
        sg.report_norm_recovery()
        assert sg._norm_failure_active  # still active

    def test_evaluate_target_state_reason_norm_only(self, sg: StormGuard) -> None:
        """Reason string should say 'Normalizer failure active' when only norm fails."""
        sg._norm_failure_active = True
        _, reason = sg._evaluate_target_state(0, 0, 0.0)
        assert "Normalizer" in reason

    def test_evaluate_target_state_reason_feature_only(self, sg: StormGuard) -> None:
        """Reason string should say 'FeatureEngine failure active' when only feature fails."""
        sg._feature_failure_active = True
        _, reason = sg._evaluate_target_state(0, 0, 0.0)
        assert "FeatureEngine" in reason

    def test_evaluate_target_state_reason_both(self, sg: StormGuard) -> None:
        """Reason string should mention both when both fail."""
        sg._norm_failure_active = True
        sg._feature_failure_active = True
        state, reason = sg._evaluate_target_state(0, 0, 0.0)
        assert state == StormGuardState.STORM
        assert "norm" in reason and "feature" in reason

    def test_norm_failure_increments_metric(self, sg: StormGuard) -> None:
        """report_norm_failure should increment norm_engine_escalation_total."""
        initial = sg.metrics.norm_engine_escalation_total._value.get()
        sg.report_norm_failure(5)
        assert sg.metrics.norm_engine_escalation_total._value.get() == initial + 1

    def test_feature_failure_does_not_increment_norm_metric(self, sg: StormGuard) -> None:
        """report_feature_failure should NOT increment norm metric."""
        initial = sg.metrics.norm_engine_escalation_total._value.get()
        sg.report_feature_failure(5)
        assert sg.metrics.norm_engine_escalation_total._value.get() == initial
