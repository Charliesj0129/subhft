"""Tests for hft_platform.alpha._validation_profile."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.alpha._validation_profile import (
    ProfileValidationError,
    ValidationProfile,
    load_profile,
)

_VALID_BODY = {
    "name": "test_strict",
    "is_strict": True,
    "thresholds": {
        "maker": {"sharpe_oos_min": 1.0, "min_fills": 300},
        "taker": {"sharpe_oos_min": 1.5},
    },
    "blocking_sub_gates": [
        "sharpe_threshold",
        "max_drawdown",
        "winning_day_pct",
    ],
}


def _write_yaml(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


class TestLoadProfile:
    def test_loads_valid_profile(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _VALID_BODY)
        prof = load_profile(p)
        assert isinstance(prof, ValidationProfile)
        assert prof.name == "test_strict"
        assert prof.is_strict is True
        assert prof.thresholds["maker"]["sharpe_oos_min"] == 1.0
        assert "sharpe_threshold" in prof.blocking_sub_gates

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_profile(tmp_path / "nope.yaml")

    def test_unregistered_gate_raises(self, tmp_path: Path) -> None:
        bad = dict(_VALID_BODY)
        bad["blocking_sub_gates"] = ["sharpe_threshold", "totally_made_up_gate"]
        p = _write_yaml(tmp_path, bad)
        with pytest.raises(ProfileValidationError, match="totally_made_up_gate"):
            load_profile(p)

    def test_strict_without_blocking_gates_raises(self, tmp_path: Path) -> None:
        bad = dict(_VALID_BODY)
        bad["blocking_sub_gates"] = []
        p = _write_yaml(tmp_path, bad)
        with pytest.raises(ProfileValidationError, match="strict profile must list"):
            load_profile(p)

    def test_non_strict_with_empty_blocking_is_ok(self, tmp_path: Path) -> None:
        body = dict(_VALID_BODY)
        body["is_strict"] = False
        body["blocking_sub_gates"] = []
        p = _write_yaml(tmp_path, body)
        prof = load_profile(p)
        assert prof.is_strict is False

    def test_thresholds_for_returns_per_gate_view(self, tmp_path: Path) -> None:
        p = _write_yaml(tmp_path, _VALID_BODY)
        prof = load_profile(p)
        merged = prof.thresholds_for(strategy_type="maker")
        assert merged["sharpe_oos_min"] == 1.0
        assert merged["min_fills"] == 300

    def test_top_level_not_mapping_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "scalar.yaml"
        p.write_text("42\n")
        with pytest.raises(ProfileValidationError, match="top-level YAML must be a mapping"):
            load_profile(p)

    def test_blocking_sub_gates_as_string_raises(self, tmp_path: Path) -> None:
        bad = dict(_VALID_BODY)
        bad["blocking_sub_gates"] = "sharpe_threshold"
        p = _write_yaml(tmp_path, bad)
        with pytest.raises(ProfileValidationError, match="blocking_sub_gates must be a list"):
            load_profile(p)

    def test_thresholds_not_mapping_raises(self, tmp_path: Path) -> None:
        bad = dict(_VALID_BODY)
        bad["thresholds"] = "not a dict"
        p = _write_yaml(tmp_path, bad)
        with pytest.raises(ProfileValidationError, match="thresholds must be a mapping"):
            load_profile(p)


class TestValidationConfigProfileField:
    def test_default_is_none(self) -> None:
        from hft_platform.alpha._validation_types import ValidationConfig

        cfg = ValidationConfig(alpha_id="x", data_paths=[])
        assert cfg.profile is None

    def test_accepts_profile_object(self, tmp_path: Path) -> None:
        from hft_platform.alpha._validation_types import ValidationConfig

        p = _write_yaml(tmp_path, _VALID_BODY)
        prof = load_profile(p)
        cfg = ValidationConfig(alpha_id="x", data_paths=[], profile=prof)
        assert cfg.profile is prof


class TestShippedStrictProfile:
    def test_vm_ul6_strict_loads(self) -> None:
        from hft_platform.alpha._validation_profile import load_profile

        prof = load_profile("config/research/profiles/vm_ul6_strict.yaml")
        assert prof.name == "vm_ul6_strict"
        assert prof.is_strict is True
        for gate_name in (
            "sharpe_threshold",
            "max_drawdown",
            "winning_day_pct",
            "fill_quality",
            "fill_rate_validation",
            "ic_evaluation",
            "min_sample_size",
            "single_day_dominance",
            "loo_day_sensitivity",
            "outlier_trade_removal",
            "day_bootstrap_ci",
            "stationary_block_bootstrap",
            "deflated_sharpe_maker",
        ):
            assert gate_name in prof.blocking_sub_gates, gate_name

    def test_strict_profile_includes_replay_parity(self) -> None:
        """Slice C task 9: strict profile must list replay_parity as blocking
        and define the replay_parity_match_pct_min threshold under BOTH the
        maker and taker sections (since the gate applies to both)."""
        from hft_platform.alpha._validation_profile import load_profile

        prof = load_profile("config/research/profiles/vm_ul6_strict.yaml")
        assert "replay_parity" in prof.blocking_sub_gates

        maker_thresholds = prof.thresholds_for(strategy_type="maker")
        taker_thresholds = prof.thresholds_for(strategy_type="taker")
        assert maker_thresholds["replay_parity_match_pct_min"] == 95.0
        assert taker_thresholds["replay_parity_match_pct_min"] == 95.0
