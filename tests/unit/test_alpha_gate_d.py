"""Unit tests for alpha Gate D — backtest quantitative thresholds."""

from __future__ import annotations

from unittest.mock import patch

from hft_platform.alpha._gate_d import _evaluate_gate_d
from hft_platform.alpha._promotion_types import PromotionConfig


def _cfg(**overrides: object) -> PromotionConfig:
    defaults = {
        "alpha_id": "test_alpha",
        "owner": "tester",
        "min_sharpe_oos": 1.0,
        "max_abs_drawdown": 0.2,
        "max_turnover": 2.0,
        "max_correlation": 0.7,
    }
    defaults.update(overrides)
    return PromotionConfig(**defaults)  # type: ignore[arg-type]


def _scorecard(
    sharpe: float | None = 1.5,
    max_drawdown: float | None = -0.10,
    turnover: float | None = 1.0,
    corr: float | None = 0.3,
    latency_profile: str | None = "sim_p95_v2026-02-26",
) -> dict:
    return {
        "sharpe_oos": sharpe,
        "max_drawdown": max_drawdown,
        "turnover": turnover,
        "correlation_pool_max": corr,
        "latency_profile": latency_profile,
    }


class TestGateDAllPass:
    def test_all_metrics_pass(self) -> None:
        passed, checks = _evaluate_gate_d(_scorecard(), _cfg())
        assert passed is True
        assert checks["sharpe_oos"]["pass"] is True
        assert checks["max_drawdown"]["pass"] is True
        assert checks["turnover"]["pass"] is True
        assert checks["correlation_pool_max"]["pass"] is True
        assert checks["latency_profile"]["pass"] is True

    def test_at_exact_thresholds(self) -> None:
        sc = _scorecard(sharpe=1.0, max_drawdown=-0.2, turnover=2.0, corr=0.7)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is True


class TestGateDSharpe:
    def test_sharpe_below_threshold_fails(self) -> None:
        sc = _scorecard(sharpe=0.9)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["sharpe_oos"]["pass"] is False

    def test_sharpe_none_fails(self) -> None:
        sc = _scorecard(sharpe=None)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["sharpe_oos"]["pass"] is False
        assert checks["sharpe_oos"]["value"] is None

    def test_sharpe_custom_threshold(self) -> None:
        sc = _scorecard(sharpe=1.8)
        passed, _ = _evaluate_gate_d(sc, _cfg(min_sharpe_oos=2.0))
        assert passed is False


class TestGateDDrawdown:
    def test_drawdown_too_deep_fails(self) -> None:
        sc = _scorecard(max_drawdown=-0.3)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["max_drawdown"]["pass"] is False

    def test_drawdown_none_fails(self) -> None:
        sc = _scorecard(max_drawdown=None)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["max_drawdown"]["pass"] is False

    def test_drawdown_threshold_uses_abs(self) -> None:
        """max_abs_drawdown is made negative internally via -abs()."""
        sc = _scorecard(max_drawdown=-0.15)
        passed, checks = _evaluate_gate_d(sc, _cfg(max_abs_drawdown=0.15))
        assert passed is True
        assert checks["max_drawdown"]["min"] == -0.15


class TestGateDTurnover:
    def test_turnover_exceeds_max_fails(self) -> None:
        sc = _scorecard(turnover=2.5)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["turnover"]["pass"] is False

    def test_turnover_none_fails(self) -> None:
        sc = _scorecard(turnover=None)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False


class TestGateDCorrelation:
    def test_correlation_exceeds_max_fails(self) -> None:
        sc = _scorecard(corr=0.8)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["correlation_pool_max"]["pass"] is False

    def test_correlation_none_fails_with_detail(self) -> None:
        sc = _scorecard(corr=None)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["correlation_pool_max"]["pass"] is False
        assert "MISSING" in checks["correlation_pool_max"]["detail"]

    def test_correlation_present_detail_ok(self) -> None:
        sc = _scorecard(corr=0.5)
        _, checks = _evaluate_gate_d(sc, _cfg())
        assert checks["correlation_pool_max"]["detail"] == "OK"


class TestGateDLatencyProfile:
    def test_missing_latency_profile_fails(self) -> None:
        sc = _scorecard(latency_profile=None)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["latency_profile"]["pass"] is False
        assert "MISSING" in checks["latency_profile"]["detail"]

    def test_empty_string_latency_profile_fails(self) -> None:
        sc = _scorecard(latency_profile="")
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["latency_profile"]["pass"] is False

    def test_present_latency_profile_passes(self) -> None:
        sc = _scorecard(latency_profile="my_profile_p95")
        _, checks = _evaluate_gate_d(sc, _cfg())
        assert checks["latency_profile"]["pass"] is True
        assert checks["latency_profile"]["detail"] == "OK"


class TestGateDFeatureSetVersion:
    def test_no_manifest_fsv_skips_check(self) -> None:
        """When manifest_feature_set_version is None, no FSV check is added."""
        sc = _scorecard()
        _, checks = _evaluate_gate_d(sc, _cfg(manifest_feature_set_version=None))
        assert "feature_set_version" not in checks

    def test_fsv_match_passes(self) -> None:
        with patch(
            "hft_platform.alpha._gate_d.FEATURE_SET_VERSION",
            "lob_shared_v1",
            create=True,
        ):
            # Patch the import inside the function
            import hft_platform.feature.registry as reg_mod

            original = getattr(reg_mod, "FEATURE_SET_VERSION", None)
            reg_mod.FEATURE_SET_VERSION = "lob_shared_v1"  # type: ignore[attr-defined]
            try:
                sc = _scorecard()
                passed, checks = _evaluate_gate_d(sc, _cfg(manifest_feature_set_version="lob_shared_v1"))
                assert "feature_set_version" in checks
                assert checks["feature_set_version"]["pass"] is True
                assert checks["feature_set_version"]["detail"] == "OK"
            finally:
                if original is not None:
                    reg_mod.FEATURE_SET_VERSION = original  # type: ignore[attr-defined]

    def test_fsv_mismatch_fails(self) -> None:
        import hft_platform.feature.registry as reg_mod

        original = getattr(reg_mod, "FEATURE_SET_VERSION", None)
        reg_mod.FEATURE_SET_VERSION = "lob_shared_v2"  # type: ignore[attr-defined]
        try:
            sc = _scorecard()
            passed, checks = _evaluate_gate_d(sc, _cfg(manifest_feature_set_version="lob_shared_v1"))
            assert "feature_set_version" in checks
            assert checks["feature_set_version"]["pass"] is False
            assert "MISMATCH" in checks["feature_set_version"]["detail"]
            # FSV mismatch blocks Gate D
            assert passed is False
        finally:
            if original is not None:
                reg_mod.FEATURE_SET_VERSION = original  # type: ignore[attr-defined]


class TestGateDMultipleFailures:
    def test_multiple_failures_all_reported(self) -> None:
        sc = _scorecard(sharpe=0.5, max_drawdown=-0.5, turnover=5.0, corr=0.9)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["sharpe_oos"]["pass"] is False
        assert checks["max_drawdown"]["pass"] is False
        assert checks["turnover"]["pass"] is False
        assert checks["correlation_pool_max"]["pass"] is False

    def test_string_numeric_values_handled(self) -> None:
        """_to_float should handle string representations."""
        sc = {
            "sharpe_oos": "1.5",
            "max_drawdown": "-0.1",
            "turnover": "1.0",
            "correlation_pool_max": "0.3",
            "latency_profile": "test",
        }
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is True
