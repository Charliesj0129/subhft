"""Tests for Gate D latency value realism checks (Unit 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from hft_platform.alpha._gate_d import (
    _check_latency_values,
    _evaluate_gate_d,
    _load_latency_profiles,
)
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


def _passing_scorecard(**overrides: Any) -> dict[str, Any]:
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
# _load_latency_profiles
# ---------------------------------------------------------------------------


class TestLoadLatencyProfiles:
    def test_loads_from_real_file(self) -> None:
        """Should load real profiles.yaml if project root is correct."""
        profiles = _load_latency_profiles("/home/charlie/hft_platform")
        assert isinstance(profiles, dict)
        # At least one shioaji profile should be present
        assert any("shioaji" in k for k in profiles)

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        profiles = _load_latency_profiles(str(tmp_path))
        assert profiles == {}

    def test_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "config" / "research"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "latency_profiles.yaml").write_text("profiles: ][invalid[")
        profiles = _load_latency_profiles(str(tmp_path))
        assert profiles == {}

    def test_empty_profiles_section(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "config" / "research"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "latency_profiles.yaml").write_text("profiles: {}\n")
        profiles = _load_latency_profiles(str(tmp_path))
        assert profiles == {}


# ---------------------------------------------------------------------------
# _check_latency_values
# ---------------------------------------------------------------------------


class TestCheckLatencyValues:
    def test_all_fields_pass(self) -> None:
        lp = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            "submit_ack_latency_ms": 36.0,
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        }
        result = _check_latency_values(lp, _PROFILES)
        assert result["pass"] is True
        assert "OK" in result["detail"]

    def test_submit_ack_below_floor_fails(self) -> None:
        lp = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            "submit_ack_latency_ms": 1.0,  # way below 36*0.8=28.8
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        }
        result = _check_latency_values(lp, _PROFILES)
        assert result["pass"] is False
        assert "UNREALISTIC" in result["detail"]
        assert "submit_ack_latency_ms" in result["detail"]

    def test_exactly_at_floor_passes(self) -> None:
        # 36.0 * 0.8 = 28.8
        lp = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            "submit_ack_latency_ms": 28.8,
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        }
        result = _check_latency_values(lp, _PROFILES)
        assert result["pass"] is True

    def test_no_profile_id_skips(self) -> None:
        lp = {"submit_ack_latency_ms": 36.0}
        result = _check_latency_values(lp, _PROFILES)
        assert result["pass"] is True
        assert result["required"] is False
        assert "SKIPPED" in result["detail"]

    def test_empty_profiles_dict_skips(self) -> None:
        lp = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            "submit_ack_latency_ms": 36.0,
        }
        result = _check_latency_values(lp, {})
        assert result["pass"] is True
        assert result["required"] is False
        assert "SKIPPED" in result["detail"]

    def test_unknown_profile_id_skips(self) -> None:
        lp = {
            "latency_profile_id": "unknown_profile_xyz",
            "submit_ack_latency_ms": 1.0,
        }
        result = _check_latency_values(lp, _PROFILES)
        assert result["pass"] is True  # non-blocking
        assert "SKIPPED" in result["detail"]

    def test_missing_field_skips_that_field(self) -> None:
        lp = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            # submit_ack_latency_ms missing
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        }
        result = _check_latency_values(lp, _PROFILES)
        assert result["pass"] is True
        assert result["field_results"]["submit_ack_latency_ms"]["skipped"] is True


# ---------------------------------------------------------------------------
# _evaluate_gate_d with latency checks
# ---------------------------------------------------------------------------


class TestEvaluateGateDLatencyValidation:
    def test_realistic_values_pass(self) -> None:
        sc = _passing_scorecard()
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is True
        assert checks["latency_values_realistic"]["pass"] is True

    def test_unrealistic_submit_ack_fails_gate_d(self) -> None:
        sc = _passing_scorecard()
        sc["latency_profile"] = {
            "latency_profile_id": "shioaji_sim_p95_v2026-03-04",
            "submit_ack_latency_ms": 0.5,  # absurdly low
            "modify_ack_latency_ms": 43.0,
            "cancel_ack_latency_ms": 47.0,
        }
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is False
        assert checks["latency_values_realistic"]["pass"] is False
        assert "UNREALISTIC" in checks["latency_values_realistic"]["detail"]

    def test_string_latency_profile_skips_value_check(self) -> None:
        sc = _passing_scorecard()
        sc["latency_profile"] = "shioaji_sim_p95_v2026-03-04"
        passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is True
        assert "latency_values_realistic" not in checks

    def test_missing_profiles_yaml_does_not_block(self, tmp_path: Path) -> None:
        sc = _passing_scorecard()
        # project_root with no latency_profiles.yaml
        cfg = _cfg(project_root=str(tmp_path))
        passed, checks = _evaluate_gate_d(sc, cfg)
        # Should pass because profiles file missing = SKIPPED (non-blocking)
        assert passed is True
        assert checks["latency_values_realistic"]["pass"] is True
        assert "SKIPPED" in checks["latency_values_realistic"]["detail"]

    def test_stress_test_warn_only(self) -> None:
        """Missing stress_test should warn but not block."""
        sc = _passing_scorecard()
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is True
        st = checks["stress_test_validated"]
        assert st["pass"] is True
        assert st["required"] is False
        assert "WARN" in st["detail"]

    def test_stress_test_present_shows_ok(self) -> None:
        sc = _passing_scorecard()
        sc["stress_test"] = {"passed": True}
        with patch("hft_platform.alpha._gate_d._load_latency_profiles", return_value=_PROFILES):
            passed, checks = _evaluate_gate_d(sc, _cfg())
        assert passed is True
        assert checks["stress_test_validated"]["value"] is True
        assert "OK" in checks["stress_test_validated"]["detail"]
