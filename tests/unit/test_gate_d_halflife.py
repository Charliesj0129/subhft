"""Tests for Gate D half-life vs broker RTT check (Unit 2)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

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
    return PromotionConfig(**defaults)


def _base_scorecard(**overrides: Any) -> dict[str, Any]:
    sc: dict[str, Any] = {
        "sharpe_oos": 1.5,
        "max_drawdown": -0.10,
        "turnover": 1.0,
        "correlation_pool_max": 0.3,
        "latency_profile": {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            "submit_ack_latency_ms": 36.0,
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        },
    }
    sc.update(overrides)
    return sc


_PROFILES: dict[str, Any] = {
    "shioaji_sim_p95_v2026-03-04": {
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    }
}


# ---------------------------------------------------------------------------
# halflife_vs_rtt tests
# ---------------------------------------------------------------------------


class TestHalflifeVsRTT:
    """Gate D Unit 2: half-life vs broker RTT check."""

    def test_halflife_10ms_submit_36ms_fails(self) -> None:
        """10ms halflife < 36*2=72ms threshold — should fail."""
        sc = _base_scorecard(signal_halflife_ms=10.0)
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        hl = checks["halflife_vs_rtt"]
        assert hl["pass"] is False
        assert hl["required"] is True
        assert "FAIL" in hl["detail"]
        assert hl["threshold_ms"] == pytest.approx(72.0)
        assert passed is False

    def test_halflife_100ms_submit_36ms_passes(self) -> None:
        """100ms halflife >= 36*2=72ms threshold — should pass."""
        sc = _base_scorecard(signal_halflife_ms=100.0)
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        hl = checks["halflife_vs_rtt"]
        assert hl["pass"] is True
        assert "OK" in hl["detail"]

    def test_halflife_exactly_threshold_passes(self) -> None:
        """Exactly at threshold (72ms) should pass."""
        sc = _base_scorecard(signal_halflife_ms=72.0)
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        hl = checks["halflife_vs_rtt"]
        assert hl["pass"] is True

    def test_missing_halflife_is_warn_only(self) -> None:
        """Missing signal_halflife_ms is warn-only, non-blocking."""
        sc = _base_scorecard()
        # No signal_halflife_ms in scorecard
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        hl = checks["halflife_vs_rtt"]
        assert hl["pass"] is True
        assert hl["required"] is False
        assert "WARN" in hl["detail"]
        assert "signal_halflife_ms" in hl["detail"]

    def test_missing_submit_ack_is_warn_only(self) -> None:
        """Missing submit_ack in latency_profile is warn-only."""
        sc = _base_scorecard(signal_halflife_ms=50.0)
        sc["latency_profile"] = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            # no submit_ack_latency_ms
            "modify_ack_latency_ms": 43.0,
        }
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        hl = checks["halflife_vs_rtt"]
        assert hl["pass"] is True
        assert hl["required"] is False
        assert "WARN" in hl["detail"]

    def test_missing_both_halflife_and_submit_ack_is_warn_only(self) -> None:
        """Both missing: warn-only."""
        sc = _base_scorecard()
        sc["latency_profile"] = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
        }
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        hl = checks["halflife_vs_rtt"]
        assert hl["pass"] is True
        assert "WARN" in hl["detail"]

    def test_string_latency_profile_missing_ack_is_warn_only(self) -> None:
        """String latency_profile means no submit_ack_latency_ms available."""
        sc = _base_scorecard(signal_halflife_ms=10.0)
        sc["latency_profile"] = "shioaji_sim_p95_v2026-03-04"
        passed, checks = _evaluate_gate_d(sc, _cfg())
        hl = checks["halflife_vs_rtt"]
        assert hl["pass"] is True
        assert hl["required"] is False

    def test_adjusted_sharpe_diagnostic_present(self) -> None:
        """adjusted_sharpe_2x_latency diagnostic is always present and non-blocking."""
        sc = _base_scorecard(signal_halflife_ms=100.0)
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        adj = checks["adjusted_sharpe_2x_latency"]
        assert adj["pass"] is True
        assert adj["value"] == pytest.approx(1.5 * 0.7)
        assert "diagnostic" in adj["detail"]

    def test_halflife_fails_makes_overall_gate_d_fail(self) -> None:
        """When halflife check fails, overall Gate D must fail."""
        sc = _base_scorecard(signal_halflife_ms=5.0)  # very short
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["halflife_vs_rtt"]["pass"] is False
