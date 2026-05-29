"""Unit tests for the Gate C blocking-subset aggregator."""

from __future__ import annotations

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import ValidationProfile

_R47_FINGERPRINT = {
    "run_id": "test",
    "config_hash": "test",
    "instrument": "TMFD6",
    "strategy_name": "r47",
    "engine": "maker_engine",
    "queue_model": "QueueDepletionFill",
    "calibration_profile_id": "uncalibrated",
    "data_source": "ck",
    "latency_profile": "shioaji_measured_p95",
    "pnl_pts": 2253.0,
    "n_fills": 39,
    "n_trading_days": 31,
    "equity_curve": None,
    "pnl_per_fill": 61.5,
    "adverse_fill_pct": 0.30,
    "fill_rate_per_day": 1.26,
    "daily_pnl": [2325.0] + [-2.4] * 30,
}


class TestInvokeSubGatesBlocking:
    def test_no_profile_returns_no_blocking_aggregate(self) -> None:
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_R47_FINGERPRINT,
            thresholds={"sharpe_is_min": 0.5, "winning_day_pct_min": 55},
            profile=None,
        )
        assert isinstance(advisory, list) and len(advisory) > 0
        assert blocking is None

    def test_strict_profile_aggregates_named_gates_to_false_for_r47(self) -> None:
        prof = ValidationProfile(
            name="test_strict",
            is_strict=True,
            thresholds={
                "maker": {
                    "min_fills": 300,
                    "min_days": 60,
                    "outlier_day_contribution_max_pct": 25.0,
                    "loo_day_sign_preserved": True,
                }
            },
            blocking_sub_gates=(
                "min_sample_size",
                "single_day_dominance",
                "loo_day_sensitivity",
            ),
        )
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_R47_FINGERPRINT,
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None
        assert blocking["passed"] is False
        failing = {f["name"] for f in blocking["failing"]}
        assert "min_sample_size" in failing
        assert "single_day_dominance" in failing
        assert "loo_day_sensitivity" in failing

    def test_strict_profile_passes_for_robust_payload(self) -> None:
        prof = ValidationProfile(
            name="test_strict",
            is_strict=True,
            thresholds={
                "maker": {
                    "min_fills": 100,
                    "min_days": 30,
                    "outlier_day_contribution_max_pct": 25.0,
                    "loo_day_sign_preserved": True,
                }
            },
            blocking_sub_gates=("min_sample_size", "single_day_dominance", "loo_day_sensitivity"),
        )
        robust = dict(_R47_FINGERPRINT)
        robust["n_fills"] = 300
        robust["n_trading_days"] = 60
        robust["daily_pnl"] = [10.0] * 60
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=robust,
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None and blocking["passed"] is True
        assert blocking["triage_status"] == "passed"
        assert blocking["triage_reasons"] == []


class TestBlockingTriageStatus:
    """Goal §4 — sample-insufficient runs are NOT generic KILLs."""

    def _profile(self, blocking: tuple[str, ...]) -> ValidationProfile:
        return ValidationProfile(
            name="test_strict",
            is_strict=True,
            thresholds={"maker": {"min_fills": 300, "min_days": 60}},
            blocking_sub_gates=blocking,
        )

    def _payload(self, *, fills: int, days: int) -> dict:
        # Robust shape on every axis except sample-size, so only
        # min_sample_size can fail.
        p = dict(_R47_FINGERPRINT)
        p["n_fills"] = fills
        p["n_trading_days"] = days
        p["daily_pnl"] = [10.0] * max(days, 1)
        return p

    def test_status_sample_promising_when_close_to_threshold(self) -> None:
        # 240/300 = 0.80, 50/60 = 0.83 → promising
        prof = self._profile(("min_sample_size",))
        _, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=self._payload(fills=240, days=50),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None
        assert blocking["passed"] is False
        assert blocking["triage_status"] == "sample_promising"
        assert blocking["triage_reasons"] == ["min_sample_size"]

    def test_status_sample_needs_more_when_well_below(self) -> None:
        prof = self._profile(("min_sample_size",))
        _, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=self._payload(fills=60, days=20),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None
        assert blocking["triage_status"] == "sample_needs_more_sample"

    def test_status_sample_inconclusive_when_zero_fills(self) -> None:
        prof = self._profile(("min_sample_size",))
        _, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=self._payload(fills=0, days=20),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None
        assert blocking["triage_status"] == "sample_inconclusive"

    def test_status_killed_when_non_sample_gate_also_fails(self) -> None:
        # min_sample_size fails AND single_day_dominance fails (R47
        # fingerprint has 2325 of 2253 from one day) → real KILL, not a
        # sample-triage routing.
        prof = ValidationProfile(
            name="test_strict",
            is_strict=True,
            thresholds={
                "maker": {
                    "min_fills": 300,
                    "min_days": 60,
                    "outlier_day_contribution_max_pct": 25.0,
                }
            },
            blocking_sub_gates=("min_sample_size", "single_day_dominance"),
        )
        _, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_R47_FINGERPRINT,
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None
        assert blocking["triage_status"] == "killed"
        assert "single_day_dominance" in blocking["triage_reasons"]
