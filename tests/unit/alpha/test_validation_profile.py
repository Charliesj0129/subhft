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
