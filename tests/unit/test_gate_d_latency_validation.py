"""Unit tests for Gate D — latency value validation and stress_test checks.

Tests cover:
- latency_values_realistic: zero-latency fails, below-floor fails, valid passes
- missing latency_profiles.yaml degrades gracefully (non-blocking)
- stress_test_validated: missing / failed is warn-only (non-blocking, pass always True)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hft_platform.alpha._gate_d import (
    _check_latency_values,
    _evaluate_gate_d,
    _load_latency_profiles,
)
from hft_platform.alpha._promotion_types import PromotionConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROFILE_ID = "shioaji_sim_p95_v2026-03-04"
_PROFILE_VALUES = {
    "submit_ack_latency_ms": 36.0,
    "modify_ack_latency_ms": 43.0,
    "cancel_ack_latency_ms": 47.0,
}


def _cfg(project_root: str = ".", **overrides: Any) -> PromotionConfig:
    defaults: dict[str, Any] = {
        "alpha_id": "test_alpha",
        "owner": "tester",
        "project_root": project_root,
        "min_sharpe_oos": 1.0,
        "max_abs_drawdown": 0.2,
        "max_turnover": 2.0,
        "max_correlation": 0.7,
    }
    defaults.update(overrides)
    return PromotionConfig(**defaults)  # type: ignore[arg-type]


def _base_scorecard(
    latency_profile: Any = None,
    stress_test: Any = None,
) -> dict[str, Any]:
    """Return a scorecard that passes all non-latency checks."""
    sc: dict[str, Any] = {
        "sharpe_oos": 1.5,
        "max_drawdown": -0.10,
        "turnover": 1.0,
        "correlation_pool_max": 0.3,
    }
    if latency_profile is not None:
        sc["latency_profile"] = latency_profile
    if stress_test is not None:
        sc["stress_test"] = stress_test
    return sc


def _realistic_latency_profile() -> dict[str, Any]:
    """A latency_profile dict with values matching the P95 profile exactly."""
    return {
        "latency_profile_id": _PROFILE_ID,
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
        "model_applied": True,
    }


def _write_profiles_yaml(tmp_path: Path, profiles: dict[str, Any]) -> Path:
    config_dir = tmp_path / "config" / "research"
    config_dir.mkdir(parents=True)
    profiles_file = config_dir / "latency_profiles.yaml"
    profiles_file.write_text(yaml.safe_dump({"profiles": profiles}))
    return profiles_file


def _standard_profiles(tmp_path: Path) -> Path:
    return _write_profiles_yaml(
        tmp_path,
        {
            _PROFILE_ID: {
                "description": "Shioaji sim P95",
                "submit_ack_latency_ms": _PROFILE_VALUES["submit_ack_latency_ms"],
                "modify_ack_latency_ms": _PROFILE_VALUES["modify_ack_latency_ms"],
                "cancel_ack_latency_ms": _PROFILE_VALUES["cancel_ack_latency_ms"],
                "local_decision_pipeline_latency_us": 250,
                "measurement_date": "2026-03-04",
            }
        },
    )


# ---------------------------------------------------------------------------
# Tests: _load_latency_profiles
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
# Tests: _check_latency_values — unit-level
# ---------------------------------------------------------------------------


class TestCheckLatencyValues:
    def test_zero_latency_fails(self) -> None:
        """Backtest with 0ms latency is below the 80% floor — must fail."""
        lp = {
            "latency_profile_id": _PROFILE_ID,
            "submit_ack_latency_ms": 0.0,
            "modify_ack_latency_ms": 0.0,
            "cancel_ack_latency_ms": 0.0,
        }
        profiles = {_PROFILE_ID: dict(_PROFILE_VALUES)}
        result = _check_latency_values(lp, profiles)
        assert result["pass"] is False
        assert "UNREALISTIC" in result["detail"]

    def test_below_floor_latency_fails(self) -> None:
        """Latency at 50% of profile (below 80% floor) must fail."""
        lp = {
            "latency_profile_id": _PROFILE_ID,
            "submit_ack_latency_ms": _PROFILE_VALUES["submit_ack_latency_ms"] * 0.5,
            "modify_ack_latency_ms": _PROFILE_VALUES["modify_ack_latency_ms"] * 0.5,
            "cancel_ack_latency_ms": _PROFILE_VALUES["cancel_ack_latency_ms"] * 0.5,
        }
        profiles = {_PROFILE_ID: dict(_PROFILE_VALUES)}
        result = _check_latency_values(lp, profiles)
        assert result["pass"] is False
        assert "UNREALISTIC" in result["detail"]
        # All three fields should be flagged
        for field in ("submit_ack_latency_ms", "modify_ack_latency_ms", "cancel_ack_latency_ms"):
            assert result["field_results"][field]["pass"] is False

    def test_exactly_at_floor_passes(self) -> None:
        """Latency at exactly 80% of profile is at the floor — should pass."""
        lp = {
            "latency_profile_id": _PROFILE_ID,
            "submit_ack_latency_ms": _PROFILE_VALUES["submit_ack_latency_ms"] * 0.8,
            "modify_ack_latency_ms": _PROFILE_VALUES["modify_ack_latency_ms"] * 0.8,
            "cancel_ack_latency_ms": _PROFILE_VALUES["cancel_ack_latency_ms"] * 0.8,
        }
        profiles = {_PROFILE_ID: dict(_PROFILE_VALUES)}
        result = _check_latency_values(lp, profiles)
        assert result["pass"] is True
        assert result["detail"].startswith("OK")

    def test_valid_realistic_latency_passes(self) -> None:
        """Latency matching profile values exactly always passes."""
        lp = {
            "latency_profile_id": _PROFILE_ID,
            **_PROFILE_VALUES,
        }
        profiles = {_PROFILE_ID: dict(_PROFILE_VALUES)}
        result = _check_latency_values(lp, profiles)
        assert result["pass"] is True
        assert "OK" in result["detail"]
        assert result["profile_id"] == _PROFILE_ID

    def test_missing_profile_id_key_is_non_blocking(self) -> None:
        """latency_profile dict with no latency_profile_id key: cannot identify profile,
        so degrade gracefully — pass=True, required=False (non-blocking)."""
        lp = {"submit_ack_latency_ms": 36.0}
        result = _check_latency_values(lp, {_PROFILE_ID: dict(_PROFILE_VALUES)})
        assert result["pass"] is True
        assert result["required"] is False
        assert "SKIPPED" in result["detail"]
        assert result["profile_id"] is None

    def test_empty_profiles_dict_is_non_blocking(self) -> None:
        """Empty profiles dict (YAML unavailable) should not block — pass=True."""
        lp = {"latency_profile_id": _PROFILE_ID, **_PROFILE_VALUES}
        result = _check_latency_values(lp, {})
        assert result["pass"] is True
        assert result["required"] is False
        assert "SKIPPED" in result["detail"]

    def test_unknown_profile_id_is_non_blocking(self) -> None:
        """Profile ID not found in YAML should not block — pass=True."""
        lp = {"latency_profile_id": "nonexistent_profile_v1", **_PROFILE_VALUES}
        profiles = {_PROFILE_ID: dict(_PROFILE_VALUES)}
        result = _check_latency_values(lp, profiles)
        assert result["pass"] is True
        assert result["required"] is False
        assert "SKIPPED" in result["detail"]

    def test_partial_failure_reports_only_failing_fields(self) -> None:
        """Only the field below floor should appear in failures."""
        lp = {
            "latency_profile_id": _PROFILE_ID,
            "submit_ack_latency_ms": 1.0,  # below floor
            "modify_ack_latency_ms": _PROFILE_VALUES["modify_ack_latency_ms"],  # OK
            "cancel_ack_latency_ms": _PROFILE_VALUES["cancel_ack_latency_ms"],  # OK
        }
        profiles = {_PROFILE_ID: dict(_PROFILE_VALUES)}
        result = _check_latency_values(lp, profiles)
        assert result["pass"] is False
        assert result["field_results"]["submit_ack_latency_ms"]["pass"] is False
        assert result["field_results"]["modify_ack_latency_ms"]["pass"] is True
        assert result["field_results"]["cancel_ack_latency_ms"]["pass"] is True


# ---------------------------------------------------------------------------
# Tests: _evaluate_gate_d integration — latency_values_realistic
# ---------------------------------------------------------------------------


class TestGateDLatencyValuesRealistic:
    def test_zero_latency_fails_gate_d(self, tmp_path: Path) -> None:
        """Zero latency in scorecard latency_profile dict should fail Gate D."""
        _standard_profiles(tmp_path)
        lp = {
            "latency_profile_id": _PROFILE_ID,
            "submit_ack_latency_ms": 0.0,
            "modify_ack_latency_ms": 0.0,
            "cancel_ack_latency_ms": 0.0,
        }
        sc = _base_scorecard(latency_profile=lp)
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        assert passed is False
        assert "latency_values_realistic" in checks
        assert checks["latency_values_realistic"]["pass"] is False
        assert "UNREALISTIC" in checks["latency_values_realistic"]["detail"]

    def test_below_floor_latency_fails_gate_d(self, tmp_path: Path) -> None:
        """Latency below 80% floor must cause Gate D to fail."""
        _standard_profiles(tmp_path)
        lp = {
            "latency_profile_id": _PROFILE_ID,
            "submit_ack_latency_ms": 5.0,   # well below 36 * 0.8 = 28.8
            "modify_ack_latency_ms": 5.0,   # well below 43 * 0.8 = 34.4
            "cancel_ack_latency_ms": 5.0,   # well below 47 * 0.8 = 37.6
        }
        sc = _base_scorecard(latency_profile=lp)
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        assert passed is False
        assert checks["latency_values_realistic"]["pass"] is False

    def test_valid_latency_passes_gate_d(self, tmp_path: Path) -> None:
        """Scorecard with realistic latency values passes Gate D."""
        _standard_profiles(tmp_path)
        lp = _realistic_latency_profile()
        sc = _base_scorecard(latency_profile=lp)
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        assert passed is True
        assert checks["latency_values_realistic"]["pass"] is True
        assert "OK" in checks["latency_values_realistic"]["detail"]

    def test_missing_profiles_yaml_does_not_fail_gate_d(self, tmp_path: Path) -> None:
        """Missing latency_profiles.yaml degrades gracefully — Gate D must not fail due to it."""
        # Do NOT create the profiles YAML in tmp_path
        lp = _realistic_latency_profile()
        sc = _base_scorecard(latency_profile=lp)
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        # latency_values_realistic should be present but non-blocking (pass=True)
        assert "latency_values_realistic" in checks
        assert checks["latency_values_realistic"]["pass"] is True
        assert checks["latency_values_realistic"]["required"] is False
        assert "SKIPPED" in checks["latency_values_realistic"]["detail"]
        # Full Gate D should pass (other checks are OK)
        assert passed is True

    def test_string_type_latency_profile_skips_value_check(self, tmp_path: Path) -> None:
        """When latency_profile is a plain string (old format), no value check is added."""
        _standard_profiles(tmp_path)
        sc = _base_scorecard(latency_profile="shioaji_sim_p95_v2026-03-04")
        _, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        # latency_values_realistic should NOT be injected for string profiles
        assert "latency_values_realistic" not in checks

    def test_latency_values_check_uses_project_root(self, tmp_path: Path) -> None:
        """Confirm profiles are loaded relative to config.project_root."""
        _standard_profiles(tmp_path)
        lp = _realistic_latency_profile()
        sc = _base_scorecard(latency_profile=lp)
        # project_root=str(tmp_path) — the profiles YAML lives there
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        assert checks["latency_values_realistic"]["pass"] is True


# ---------------------------------------------------------------------------
# Tests: _evaluate_gate_d integration — stress_test_validated
# ---------------------------------------------------------------------------


class TestGateDStressTestValidated:
    def test_missing_stress_test_is_warn_only(self, tmp_path: Path) -> None:
        """stress_test absent from scorecard should not block Gate D (pass always True)."""
        _standard_profiles(tmp_path)
        lp = _realistic_latency_profile()
        sc = _base_scorecard(latency_profile=lp)
        # No stress_test key
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        assert "stress_test_validated" in checks
        stress_chk = checks["stress_test_validated"]
        assert stress_chk["required"] is False
        # pass is always True (non-blocking); actual result is in "value"
        assert stress_chk["pass"] is True
        assert stress_chk["value"] is False  # was not passed
        # Gate D passes regardless
        assert passed is True

    def test_stress_test_failed_is_warn_only(self, tmp_path: Path) -> None:
        """stress_test.passed=False should not block Gate D (pass always True)."""
        _standard_profiles(tmp_path)
        lp = _realistic_latency_profile()
        sc = _base_scorecard(latency_profile=lp, stress_test={"passed": False})
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        stress_chk = checks["stress_test_validated"]
        assert stress_chk["required"] is False
        assert stress_chk["pass"] is True  # non-blocking
        assert stress_chk["value"] is False  # actual stress result
        assert "WARN" in stress_chk["detail"]
        # Non-blocking: Gate D still passes
        assert passed is True

    def test_stress_test_passed_sets_value_true(self, tmp_path: Path) -> None:
        """stress_test.passed=True should record value=True and detail OK."""
        _standard_profiles(tmp_path)
        lp = _realistic_latency_profile()
        sc = _base_scorecard(latency_profile=lp, stress_test={"passed": True})
        _, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        stress_chk = checks["stress_test_validated"]
        assert stress_chk["pass"] is True
        assert stress_chk["value"] is True
        assert stress_chk["detail"] == "OK"

    def test_stress_test_check_always_present(self, tmp_path: Path) -> None:
        """stress_test_validated should always appear in the checks dict."""
        _standard_profiles(tmp_path)
        sc: dict[str, Any] = {
            "sharpe_oos": 1.5,
            "max_drawdown": -0.1,
            "turnover": 1.0,
            "correlation_pool_max": 0.3,
        }
        _, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        assert "stress_test_validated" in checks

    def test_stress_test_none_is_non_blocking(self, tmp_path: Path) -> None:
        """stress_test=None in scorecard is treated as absent — non-blocking (pass True)."""
        _standard_profiles(tmp_path)
        lp = _realistic_latency_profile()
        sc = _base_scorecard(latency_profile=lp)
        sc["stress_test"] = None
        passed, checks = _evaluate_gate_d(sc, _cfg(project_root=str(tmp_path)))
        assert checks["stress_test_validated"]["pass"] is True  # non-blocking
        assert checks["stress_test_validated"]["value"] is False
        assert passed is True
