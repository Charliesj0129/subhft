"""Unit tests for Gate D latency_profile_id_known check."""

from __future__ import annotations

from unittest.mock import patch

from hft_platform.alpha._gate_d import _evaluate_gate_d
from hft_platform.alpha._promotion_types import PromotionConfig

_PROFILES = {
    "sim_p95_v2026-02-26": {
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    },
}

_BASE_SCORECARD = {
    "sharpe_oos": 1.5,
    "max_drawdown": -0.05,
    "latency_profile": {
        "latency_profile_id": "sim_p95_v2026-02-26",
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
    },
    "alpha_half_life_seconds": 120.0,
}


def _default_config() -> PromotionConfig:
    return PromotionConfig(alpha_id="test", owner="test_owner")


class TestGateDLatencyProfileIdKnown:
    @patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES)
    def test_known_id_shows_ok(self, mock_load):
        _passed, checks = _evaluate_gate_d(_BASE_SCORECARD, _default_config())
        lp_check = checks.get("latency_profile_id_known", {})
        assert lp_check.get("valid") is True
        assert lp_check.get("pass") is True

    @patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES)
    def test_unknown_id_warns_but_does_not_block(self, mock_load):
        sc = dict(_BASE_SCORECARD)
        sc["latency_profile"] = dict(sc["latency_profile"])
        sc["latency_profile"]["latency_profile_id"] = "unknown_profile"
        _passed, checks = _evaluate_gate_d(sc, _default_config())
        lp_check = checks.get("latency_profile_id_known", {})
        assert lp_check.get("valid") is False
        # Gate D should still pass (warn-only)
        assert lp_check.get("pass") is True

    @patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES)
    def test_string_latency_profile_skips_id_check(self, mock_load):
        sc = dict(_BASE_SCORECARD)
        sc["latency_profile"] = "sim_p95_v2026-02-26"
        _passed, checks = _evaluate_gate_d(sc, _default_config())
        assert "latency_profile_id_known" not in checks

    @patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES)
    def test_missing_latency_profile_id_key_skips_check(self, mock_load):
        sc = dict(_BASE_SCORECARD)
        sc["latency_profile"] = {
            "submit_ack_latency_ms": 36.0,
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        }
        _passed, checks = _evaluate_gate_d(sc, _default_config())
        assert "latency_profile_id_known" not in checks
