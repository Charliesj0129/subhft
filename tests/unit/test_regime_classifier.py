"""Tests for execution regime classifier (Direction C, R24)."""

from __future__ import annotations

from hft_platform.execution.regime_classifier import Regime, RegimeClassifier


def _make_feature_tuple(
    *,
    ret_autocov: int = 0,
    tob_survival: int = 300,
    toxicity: int = 0,
    spread_ema300s: int = 10000,
    n_features: int = 27,
) -> tuple[int, ...]:
    """Build a synthetic feature tuple with controllable values at key indices."""
    vals = [0] * n_features
    vals[17] = ret_autocov  # ret_autocov_5s_x1e6
    vals[18] = tob_survival  # tob_survival_ms
    vals[21] = toxicity  # toxicity_ema50_x1000
    if n_features > 26:
        vals[26] = spread_ema300s  # spread_ema300s
    return tuple(vals)


class TestRegimeClassifierBasic:
    """Basic classification tests."""

    def test_neutral_when_disabled(self) -> None:
        rc = RegimeClassifier(enabled=False)
        ft = _make_feature_tuple(tob_survival=10)  # would be ADVERSE if enabled
        assert rc.classify(ft) == Regime.NEUTRAL

    def test_neutral_when_no_features(self) -> None:
        rc = RegimeClassifier()
        assert rc.classify(None) == Regime.NEUTRAL

    def test_neutral_default_features(self) -> None:
        rc = RegimeClassifier()
        ft = _make_feature_tuple()  # default: tob=300, autocov=0, tox=0
        assert rc.classify(ft) == Regime.NEUTRAL


class TestAdverseClassification:
    """ADVERSE regime detection tests."""

    def test_adverse_on_burst(self) -> None:
        rc = RegimeClassifier()
        ft = _make_feature_tuple(tob_survival=1000)  # otherwise favorable
        assert rc.classify(ft, burst_active=True) == Regime.ADVERSE

    def test_adverse_on_high_toxicity(self) -> None:
        rc = RegimeClassifier(toxicity_adverse_threshold=400)
        ft = _make_feature_tuple(toxicity=500)
        assert rc.classify(ft) == Regime.ADVERSE

    def test_adverse_on_negative_high_toxicity(self) -> None:
        """Toxicity check uses absolute value (sell-side toxicity)."""
        rc = RegimeClassifier(toxicity_adverse_threshold=400)
        ft = _make_feature_tuple(toxicity=-500)
        assert rc.classify(ft) == Regime.ADVERSE

    def test_adverse_on_short_tob_survival(self) -> None:
        rc = RegimeClassifier(tob_survival_adverse_ms=50)
        ft = _make_feature_tuple(tob_survival=30)
        assert rc.classify(ft) == Regime.ADVERSE

    def test_adverse_on_wide_spread(self) -> None:
        rc = RegimeClassifier(spread_wide_threshold=50000)
        ft = _make_feature_tuple(spread_ema300s=60000)
        assert rc.classify(ft) == Regime.ADVERSE

    def test_spread_check_disabled_by_default(self) -> None:
        rc = RegimeClassifier()  # spread_wide_threshold=0
        ft = _make_feature_tuple(spread_ema300s=999999, tob_survival=300)
        assert rc.classify(ft) != Regime.ADVERSE


class TestFavorableClassification:
    """FAVORABLE regime detection tests."""

    def test_favorable_high_tob_low_autocov(self) -> None:
        rc = RegimeClassifier(
            tob_survival_favorable_ms=500,
            ret_autocov_calm_threshold=500,
        )
        ft = _make_feature_tuple(tob_survival=800, ret_autocov=100)
        assert rc.classify(ft) == Regime.FAVORABLE

    def test_not_favorable_when_tob_below_threshold(self) -> None:
        rc = RegimeClassifier(tob_survival_favorable_ms=500)
        ft = _make_feature_tuple(tob_survival=400, ret_autocov=100)
        result = rc.classify(ft)
        assert result != Regime.FAVORABLE
        assert result == Regime.NEUTRAL

    def test_not_favorable_when_autocov_high(self) -> None:
        rc = RegimeClassifier(ret_autocov_calm_threshold=500)
        ft = _make_feature_tuple(tob_survival=800, ret_autocov=600)
        assert rc.classify(ft) == Regime.NEUTRAL

    def test_not_favorable_when_negative_autocov_high(self) -> None:
        """Autocov check uses absolute value."""
        rc = RegimeClassifier(ret_autocov_calm_threshold=500)
        ft = _make_feature_tuple(tob_survival=800, ret_autocov=-600)
        assert rc.classify(ft) == Regime.NEUTRAL


class TestAdversePriority:
    """ADVERSE conditions override FAVORABLE conditions."""

    def test_adverse_overrides_favorable_tob(self) -> None:
        """Even with high tob_survival, burst forces ADVERSE."""
        rc = RegimeClassifier()
        ft = _make_feature_tuple(tob_survival=1000, ret_autocov=0)
        assert rc.classify(ft, burst_active=True) == Regime.ADVERSE

    def test_toxicity_overrides_favorable(self) -> None:
        rc = RegimeClassifier(toxicity_adverse_threshold=400)
        ft = _make_feature_tuple(tob_survival=1000, ret_autocov=0, toxicity=500)
        assert rc.classify(ft) == Regime.ADVERSE


class TestTransitionTracking:
    """Regime transition counting for observability."""

    def test_transition_count_increments(self) -> None:
        rc = RegimeClassifier(
            tob_survival_adverse_ms=50,
            tob_survival_favorable_ms=500,
            holdoff_ns=0,  # disable holdoff for this test
        )
        assert rc.transition_count == 0

        # NEUTRAL -> ADVERSE
        ft_adverse = _make_feature_tuple(tob_survival=30)
        rc.classify(ft_adverse)
        assert rc.transition_count == 1
        assert rc.last_regime == Regime.ADVERSE

        # ADVERSE -> FAVORABLE
        ft_favorable = _make_feature_tuple(tob_survival=800, ret_autocov=100)
        rc.classify(ft_favorable)
        assert rc.transition_count == 2
        assert rc.last_regime == Regime.FAVORABLE

    def test_no_transition_on_same_regime(self) -> None:
        rc = RegimeClassifier(tob_survival_adverse_ms=50, holdoff_ns=0)
        ft = _make_feature_tuple(tob_survival=30)
        rc.classify(ft)
        rc.classify(ft)
        rc.classify(ft)
        assert rc.transition_count == 1  # only first transition counted

    def test_reset_clears_state(self) -> None:
        rc = RegimeClassifier(tob_survival_adverse_ms=50, holdoff_ns=0)
        ft = _make_feature_tuple(tob_survival=30)
        rc.classify(ft)
        assert rc.transition_count == 1
        rc.reset()
        assert rc.transition_count == 0
        assert rc.last_regime == Regime.NEUTRAL


class TestHoldoffDebouncing:
    """Holdoff logic to suppress rapid transitions."""

    def test_holdoff_suppresses_rapid_transition(self) -> None:
        rc = RegimeClassifier(
            tob_survival_adverse_ms=50,
            tob_survival_favorable_ms=500,
            holdoff_ns=5_000_000_000,  # 5 seconds
        )
        t0 = 1_000_000_000_000  # base timestamp

        # First classification: ADVERSE
        ft_adverse = _make_feature_tuple(tob_survival=30)
        result = rc.classify(ft_adverse, ts_ns=t0)
        assert result == Regime.ADVERSE
        assert rc.transition_count == 1

        # Try to transition to FAVORABLE after 1 second (within holdoff)
        ft_favorable = _make_feature_tuple(tob_survival=800, ret_autocov=100)
        result = rc.classify(ft_favorable, ts_ns=t0 + 1_000_000_000)
        assert result == Regime.ADVERSE  # suppressed
        assert rc.transition_count == 1

        # After holdoff expires (6 seconds), transition should work
        result = rc.classify(ft_favorable, ts_ns=t0 + 6_000_000_000)
        assert result == Regime.FAVORABLE
        assert rc.transition_count == 2

    def test_holdoff_disabled_when_zero(self) -> None:
        rc = RegimeClassifier(
            tob_survival_adverse_ms=50,
            tob_survival_favorable_ms=500,
            holdoff_ns=0,
        )
        t0 = 1_000_000_000_000

        ft_adverse = _make_feature_tuple(tob_survival=30)
        rc.classify(ft_adverse, ts_ns=t0)
        assert rc.last_regime == Regime.ADVERSE

        ft_favorable = _make_feature_tuple(tob_survival=800, ret_autocov=100)
        result = rc.classify(ft_favorable, ts_ns=t0 + 1)  # 1ns later
        assert result == Regime.FAVORABLE  # no holdoff

    def test_holdoff_not_applied_when_no_timestamp(self) -> None:
        rc = RegimeClassifier(
            tob_survival_adverse_ms=50,
            holdoff_ns=5_000_000_000,
        )

        ft_adverse = _make_feature_tuple(tob_survival=30)
        rc.classify(ft_adverse)  # ts_ns=0 (default)
        assert rc.last_regime == Regime.ADVERSE

        ft_neutral = _make_feature_tuple(tob_survival=300)
        result = rc.classify(ft_neutral)  # ts_ns=0
        assert result == Regime.NEUTRAL  # no holdoff when ts_ns=0


class TestShortFeatureTuple:
    """Handle feature tuples shorter than expected (v2 without v3 features)."""

    def test_v2_tuple_no_spread_check(self) -> None:
        """v2 tuple has 22 features (indices 0-21), no spread_ema300s at [26]."""
        rc = RegimeClassifier(spread_wide_threshold=50000)
        ft = _make_feature_tuple(tob_survival=800, ret_autocov=100, n_features=22)
        # Should classify as FAVORABLE (spread check skipped due to short tuple)
        assert rc.classify(ft) == Regime.FAVORABLE

    def test_v2_tuple_toxicity_still_works(self) -> None:
        """v2 tuple has toxicity at [21], still within bounds."""
        rc = RegimeClassifier(toxicity_adverse_threshold=400)
        ft = _make_feature_tuple(toxicity=500, n_features=22)
        assert rc.classify(ft) == Regime.ADVERSE


class TestStatus:
    """Observability status output."""

    def test_status_contains_required_fields(self) -> None:
        rc = RegimeClassifier()
        status = rc.status()
        assert "enabled" in status
        assert "last_regime" in status
        assert "transition_count" in status
        assert "thresholds" in status
        assert isinstance(status["thresholds"], dict)

    def test_enabled_setter(self) -> None:
        rc = RegimeClassifier(enabled=True)
        assert rc.enabled is True
        rc.enabled = False
        assert rc.enabled is False
        assert rc.classify(_make_feature_tuple(tob_survival=10)) == Regime.NEUTRAL
