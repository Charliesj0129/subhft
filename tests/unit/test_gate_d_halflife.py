"""Tests for Gate D half-life vs broker RTT check (Unit 2)."""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._gate_d import _evaluate_gate_d
from hft_platform.alpha._promotion_types import PromotionConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides: Any) -> PromotionConfig:
    defaults: dict[str, Any] = {
        "alpha_id": "test_alpha",
        "owner": "tester",
        "min_sharpe_oos": 1.0,
        "max_abs_drawdown": 0.2,
        "max_turnover": 2.0,
        "max_correlation": 0.7,
    }
    defaults.update(overrides)
    return PromotionConfig(**defaults)  # type: ignore[arg-type]


def _passing_scorecard(**overrides: object) -> dict:
    """Minimal scorecard that passes all existing Gate D checks (except halflife_vs_rtt variants)."""
    base: dict = {
        "sharpe_oos": 1.5,
        "max_drawdown": -0.10,
        "turnover": 1.0,
        "correlation_pool_max": 0.3,
        # latency_profile as a dict so halflife_vs_rtt can extract submit_ack_latency_ms
        "latency_profile": {"submit_ack_latency_ms": 36.0, "profile_id": "sim_p95_v1"},
    }
    base.update(overrides)
    return base


class TestHalflifeVsRTTFails:
    def test_10ms_halflife_36ms_rtt_fails(self) -> None:
        """10ms halflife < 36*2=72ms threshold → check must fail and block Gate D."""
        sc = _passing_scorecard(signal_halflife_ms=10.0)
        passed, checks = _evaluate_gate_d(sc, _cfg())

        assert "halflife_vs_rtt" in checks
        hl_check = checks["halflife_vs_rtt"]
        assert hl_check["pass"] is False
        assert hl_check["required"] is True
        assert hl_check["value"] == 10.0
        assert hl_check["submit_ack_latency_ms"] == 36.0
        assert hl_check["threshold_ms"] == 72.0
        assert "FAIL" in hl_check["detail"]
        # Gate D overall must also fail
        assert passed is False

    def test_detail_message_contains_both_values(self) -> None:
        """Detail message should surface halflife and threshold for diagnosability."""
        sc = _passing_scorecard(signal_halflife_ms=10.0)
        _, checks = _evaluate_gate_d(sc, _cfg())
        detail = checks["halflife_vs_rtt"]["detail"]
        assert "10.00" in detail
        assert "72.00" in detail

    def test_just_below_threshold_fails(self) -> None:
        """71.9ms halflife with 36ms RTT (threshold=72) → just below boundary → fail."""
        sc = _passing_scorecard(signal_halflife_ms=71.9)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert checks["halflife_vs_rtt"]["pass"] is False
        assert passed is False


class TestHalflifeVsRTTPasses:
    def test_100ms_halflife_36ms_rtt_passes(self) -> None:
        """100ms halflife >= 36*2=72ms → check must pass."""
        sc = _passing_scorecard(signal_halflife_ms=100.0)
        passed, checks = _evaluate_gate_d(sc, _cfg())

        assert "halflife_vs_rtt" in checks
        hl_check = checks["halflife_vs_rtt"]
        assert hl_check["pass"] is True
        assert hl_check["required"] is True
        assert hl_check["value"] == 100.0
        assert hl_check["threshold_ms"] == 72.0
        assert hl_check["detail"] == "OK"
        assert passed is True

    def test_exact_threshold_passes(self) -> None:
        """Exactly 72ms halflife with 36ms RTT → equals threshold → should pass (>=)."""
        sc = _passing_scorecard(signal_halflife_ms=72.0)
        _, checks = _evaluate_gate_d(sc, _cfg())
        assert checks["halflife_vs_rtt"]["pass"] is True

    def test_large_halflife_passes(self) -> None:
        """500ms halflife with 36ms RTT → comfortably above threshold."""
        sc = _passing_scorecard(signal_halflife_ms=500.0)
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert checks["halflife_vs_rtt"]["pass"] is True
        assert passed is True


class TestHalflifeVsRTTWarnOnly:
    def test_missing_signal_halflife_ms_warn_only(self) -> None:
        """No signal_halflife_ms in scorecard → warn-only, pass=True, required=False."""
        sc = _passing_scorecard()  # no signal_halflife_ms key
        passed, checks = _evaluate_gate_d(sc, _cfg())

        assert "halflife_vs_rtt" in checks
        hl_check = checks["halflife_vs_rtt"]
        assert hl_check["pass"] is True
        assert hl_check["required"] is False
        assert hl_check["value"] is None
        assert "WARN" in hl_check["detail"]
        assert "signal_halflife_ms" in hl_check["detail"]
        # Gate D should still pass (no other failures in this scorecard)
        assert passed is True

    def test_missing_signal_halflife_detail_mentions_missing_field(self) -> None:
        sc = _passing_scorecard()
        _, checks = _evaluate_gate_d(sc, _cfg())
        assert "signal_halflife_ms" in checks["halflife_vs_rtt"]["detail"]

    def test_missing_latency_profile_warn_only(self) -> None:
        """No latency_profile dict in scorecard → halflife check is warn-only (pass=True)."""
        sc = {
            "sharpe_oos": 1.5,
            "max_drawdown": -0.10,
            "turnover": 1.0,
            "correlation_pool_max": 0.3,
            "latency_profile": None,  # absent / null
            "signal_halflife_ms": 50.0,
        }
        _, checks = _evaluate_gate_d(sc, _cfg())

        assert "halflife_vs_rtt" in checks
        hl_check = checks["halflife_vs_rtt"]
        # Cannot compute threshold without RTT — must be warn-only
        assert hl_check["pass"] is True
        assert hl_check["required"] is False
        assert "WARN" in hl_check["detail"]
        assert "submit_ack_latency_ms" in hl_check["detail"]

    def test_latency_profile_missing_submit_ack_field_warn_only(self) -> None:
        """latency_profile dict present but missing submit_ack_latency_ms → warn-only."""
        sc = _passing_scorecard(
            latency_profile={"profile_id": "sim_p95_v1"},  # no submit_ack_latency_ms
            signal_halflife_ms=50.0,
        )
        _, checks = _evaluate_gate_d(sc, _cfg())

        hl_check = checks["halflife_vs_rtt"]
        assert hl_check["pass"] is True
        assert hl_check["required"] is False
        assert "WARN" in hl_check["detail"]

    def test_both_missing_warns_about_both(self) -> None:
        """Both signal_halflife_ms and latency_profile missing → detail mentions both."""
        sc = {
            "sharpe_oos": 1.5,
            "max_drawdown": -0.10,
            "turnover": 1.0,
            "correlation_pool_max": 0.3,
            "latency_profile": None,
        }
        _, checks = _evaluate_gate_d(sc, _cfg())
        detail = checks["halflife_vs_rtt"]["detail"]
        assert "signal_halflife_ms" in detail
        assert "submit_ack_latency_ms" in detail

    def test_string_latency_profile_not_a_dict_warn_only(self) -> None:
        """A string latency_profile (legacy format) → cannot extract RTT → warn-only."""
        sc = _passing_scorecard(
            latency_profile="sim_p95_v2026-02-26",  # old string format
            signal_halflife_ms=50.0,
        )
        _, checks = _evaluate_gate_d(sc, _cfg())

        hl_check = checks["halflife_vs_rtt"]
        assert hl_check["pass"] is True
        assert hl_check["required"] is False


class TestHalflifeVsRTTIsolation:
    def test_halflife_check_does_not_affect_other_checks(self) -> None:
        """halflife_vs_rtt failure should not corrupt other check entries."""
        sc = _passing_scorecard(signal_halflife_ms=5.0)
        _, checks = _evaluate_gate_d(sc, _cfg())

        assert checks["sharpe_oos"]["pass"] is True
        assert checks["max_drawdown"]["pass"] is True
        assert checks["turnover"]["pass"] is True
        assert checks["correlation_pool_max"]["pass"] is True
        assert checks["latency_profile"]["pass"] is True
        # Only halflife_vs_rtt fails
        assert checks["halflife_vs_rtt"]["pass"] is False

    def test_string_numeric_halflife_handled_via_to_float(self) -> None:
        """signal_halflife_ms as a string → _to_float converts it correctly."""
        sc = _passing_scorecard(signal_halflife_ms="100.0")
        _, checks = _evaluate_gate_d(sc, _cfg())
        assert checks["halflife_vs_rtt"]["pass"] is True
        assert checks["halflife_vs_rtt"]["value"] == 100.0
