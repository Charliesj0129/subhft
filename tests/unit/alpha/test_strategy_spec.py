"""Tests for the strategy_spec loader + validator (Round 11 / goal §3)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from hft_platform.alpha.strategy_spec import (
    ALLOWED_FREQUENCY_CLASSES,
    ALLOWED_TIMEFRAMES,
    REQUIRED_TOP_LEVEL_FIELDS,
    is_multi_leg,
    load_spec,
    validate_spec,
)

REPO_TEMPLATE = Path("research/templates/strategy_spec.yaml")


def _valid_spec() -> dict:
    """A fully-populated, valid spec used as the baseline for mutation tests."""
    return {
        "strategy_name": "c99_txfd6_demo",
        "market": "TAIFEX",
        "instrument": "TXFD6",
        "hypothesis": "Opening-range expansion drives short-term momentum.",
        "timeframe": "5m",
        "holding_period": "intraday <2h",
        "frequency_class": "intraday_hft",
        "entry_rule": "Break of opening range high after 09:00",
        "exit_rule": "5m close back below range OR 13:25 force flat",
        "position_sizing": "fixed 1 lot (governed centrally)",
        "risk_control": {
            "max_position": 1,
            "max_drawdown_pts": 80,
            "force_flat_rule": "13:25 TPE close",
            "per_trade_stop_pts": 15,
        },
        "cost_model": {
            "fee_bps": 0.4,
            "tax_bps": 2.0,
            "slippage_pts": 0.5,
            "latency_profile": "shioaji_measured_p95",
        },
        "validation_plan": {
            "data_range": "2026-01-02..2026-05-13",
            "oos_split": "70/30 by trading day",
            "sample_targets": {
                "min_round_trips": 300,
                "min_oos_trading_days": 60,
            },
            "required_gates": [
                "min_sample_size",
                "edge_per_round_trip",
                "monthly_distribution",
            ],
            "net_edge_floor_pts": 10.0,
        },
    }


class TestRequiredFields:
    def test_baseline_spec_is_valid(self) -> None:
        errors = validate_spec(_valid_spec())
        assert errors == [], errors

    @pytest.mark.parametrize("field", REQUIRED_TOP_LEVEL_FIELDS)
    def test_each_top_level_field_required(self, field: str) -> None:
        spec = _valid_spec()
        spec.pop(field)
        errors = validate_spec(spec)
        assert any(field in e for e in errors), (field, errors)

    @pytest.mark.parametrize("field", REQUIRED_TOP_LEVEL_FIELDS)
    def test_empty_value_treated_as_missing(self, field: str) -> None:
        spec = _valid_spec()
        spec[field] = "" if isinstance(spec[field], str) else None
        errors = validate_spec(spec)
        assert any(field in e for e in errors)

    def test_zero_is_not_treated_as_empty(self) -> None:
        # max_drawdown_pts=0 is a degenerate spec but not "missing";
        # only None / "" / [] / {} count as empty so 0 must pass.
        spec = _valid_spec()
        spec["risk_control"]["max_drawdown_pts"] = 0
        errors = validate_spec(spec)
        assert all("max_drawdown_pts" not in e for e in errors)


class TestEnumValidation:
    @pytest.mark.parametrize("tf", sorted(ALLOWED_TIMEFRAMES))
    def test_each_allowed_timeframe_passes(self, tf: str) -> None:
        spec = _valid_spec()
        spec["timeframe"] = tf
        assert validate_spec(spec) == []

    def test_unknown_timeframe_rejected(self) -> None:
        spec = _valid_spec()
        spec["timeframe"] = "30m"
        errors = validate_spec(spec)
        assert any("timeframe" in e for e in errors)

    @pytest.mark.parametrize("fc", sorted(ALLOWED_FREQUENCY_CLASSES))
    def test_each_allowed_frequency_class_passes(self, fc: str) -> None:
        spec = _valid_spec()
        spec["frequency_class"] = fc
        assert validate_spec(spec) == []

    def test_unknown_frequency_class_rejected(self) -> None:
        spec = _valid_spec()
        spec["frequency_class"] = "weekly"
        errors = validate_spec(spec)
        assert any("frequency_class" in e for e in errors)

    def test_unknown_market_rejected(self) -> None:
        spec = _valid_spec()
        spec["market"] = "CME"
        errors = validate_spec(spec)
        assert any("market" in e for e in errors)


class TestInstrumentShape:
    def test_single_leg_string(self) -> None:
        spec = _valid_spec()
        spec["instrument"] = "TMFD6"
        assert validate_spec(spec) == []
        assert is_multi_leg(spec) is False

    def test_multi_leg_list(self) -> None:
        spec = _valid_spec()
        spec["instrument"] = ["TXOD6_C18000", "TXOD6_P18000"]
        assert validate_spec(spec) == []
        assert is_multi_leg(spec) is True

    def test_length_one_list_flagged(self) -> None:
        spec = _valid_spec()
        spec["instrument"] = ["TXFD6"]
        errors = validate_spec(spec)
        assert any("instrument" in e for e in errors)

    def test_non_string_leg_rejected(self) -> None:
        spec = _valid_spec()
        spec["instrument"] = ["TXOD6_C18000", 123]  # type: ignore[list-item]
        errors = validate_spec(spec)
        assert any("instrument[1]" in e for e in errors)

    def test_invalid_type_rejected(self) -> None:
        spec = _valid_spec()
        spec["instrument"] = {"leg1": "TXFD6"}  # type: ignore[assignment]
        errors = validate_spec(spec)
        assert any("instrument" in e for e in errors)


class TestSubBlocks:
    @pytest.mark.parametrize("field", ["max_position", "max_drawdown_pts", "force_flat_rule"])
    def test_risk_control_required_fields(self, field: str) -> None:
        spec = _valid_spec()
        spec["risk_control"].pop(field)
        errors = validate_spec(spec)
        assert any(f"risk_control.{field}" in e for e in errors)

    @pytest.mark.parametrize("field", ["fee_bps", "tax_bps", "slippage_pts", "latency_profile"])
    def test_cost_model_required_fields(self, field: str) -> None:
        spec = _valid_spec()
        spec["cost_model"].pop(field)
        errors = validate_spec(spec)
        assert any(f"cost_model.{field}" in e for e in errors)

    @pytest.mark.parametrize("field", ["min_round_trips", "min_oos_trading_days"])
    def test_sample_targets_required(self, field: str) -> None:
        spec = _valid_spec()
        spec["validation_plan"]["sample_targets"].pop(field)
        errors = validate_spec(spec)
        assert any(f"sample_targets.{field}" in e for e in errors)

    def test_empty_required_gates_rejected(self) -> None:
        spec = _valid_spec()
        spec["validation_plan"]["required_gates"] = []
        errors = validate_spec(spec)
        assert any("required_gates" in e for e in errors)


class TestNetEdgeFloorGuard:
    def test_floor_below_10_rejected(self) -> None:
        # 限制 §3: cannot relax the > 10 pts/trade bar.
        spec = _valid_spec()
        spec["validation_plan"]["net_edge_floor_pts"] = 5.0
        errors = validate_spec(spec)
        assert any("net_edge_floor_pts" in e for e in errors)
        assert any("10" in e for e in errors)

    def test_floor_exactly_10_passes(self) -> None:
        spec = _valid_spec()
        spec["validation_plan"]["net_edge_floor_pts"] = 10.0
        assert validate_spec(spec) == []

    def test_floor_above_10_passes(self) -> None:
        spec = _valid_spec()
        spec["validation_plan"]["net_edge_floor_pts"] = 15.0
        assert validate_spec(spec) == []


class TestLoader:
    def test_load_repo_template_returns_dict(self) -> None:
        spec = load_spec(REPO_TEMPLATE)
        assert isinstance(spec, dict)
        # Template is intentionally blank in every required field — it's
        # a starting point, not a valid candidate — so it should fail.
        errors = validate_spec(spec)
        assert len(errors) > 0

    def test_load_round_trip(self, tmp_path: Path) -> None:
        spec = _valid_spec()
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump(spec), encoding="utf-8")
        loaded = load_spec(path)
        assert validate_spec(loaded) == []

    def test_load_non_mapping_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_spec(path)


class TestDefensive:
    def test_non_dict_spec_returns_error(self) -> None:
        errors = validate_spec("not a mapping")  # type: ignore[arg-type]
        assert errors == ["spec must be a mapping"]

    def test_does_not_mutate_input(self) -> None:
        spec = _valid_spec()
        before = copy.deepcopy(spec)
        validate_spec(spec)
        assert spec == before


# --- Round 18: extract_provenance helper (goal §4) ------------------

from hft_platform.alpha.strategy_spec import extract_provenance


class TestExtractProvenance:
    def test_full_spec_round_trips_to_provenance_triple(self) -> None:
        prov = extract_provenance(_valid_spec())
        assert prov["data_range"] == "2026-01-02..2026-05-13"
        assert "shioaji_measured_p95" in prov["cost_model_id"]
        assert "0.4bp" in prov["cost_model_id"]
        assert "2.0bp" in prov["cost_model_id"]
        assert "0.5pts" in prov["cost_model_id"]
        assert prov["required_gates"] == [
            "min_sample_size",
            "edge_per_round_trip",
            "monthly_distribution",
        ]

    def test_missing_cost_model_yields_empty_id(self) -> None:
        spec = _valid_spec()
        spec.pop("cost_model")
        prov = extract_provenance(spec)
        assert prov["cost_model_id"] == ""

    def test_non_list_required_gates_filtered_to_empty(self) -> None:
        spec = _valid_spec()
        spec["validation_plan"]["required_gates"] = "not_a_list"
        prov = extract_provenance(spec)
        assert prov["required_gates"] == []

    def test_non_string_required_gates_dropped(self) -> None:
        spec = _valid_spec()
        spec["validation_plan"]["required_gates"] = ["min_sample_size", None, 7]
        prov = extract_provenance(spec)
        # Strings kept, ints coerced, None dropped (non str/int/float).
        assert "min_sample_size" in prov["required_gates"]
        assert "7" in prov["required_gates"]
        assert None not in prov["required_gates"]

    def test_non_dict_spec_returns_empty_shape(self) -> None:
        prov = extract_provenance("not a dict")  # type: ignore[arg-type]
        assert prov == {"data_range": "", "cost_model_id": "", "required_gates": []}

    def test_timeframe_and_holding_period_carried_when_present(self) -> None:
        # Round 72: additive §3 fields land in provenance when the spec has them.
        prov = extract_provenance(_valid_spec())
        assert prov["timeframe"] == "5m"
        assert prov["holding_period"] == "intraday <2h"

    def test_missing_timeframe_omits_key(self) -> None:
        # Back-compatible: absent/empty fields are not added, triple shape kept.
        spec = _valid_spec()
        spec.pop("timeframe")
        spec["holding_period"] = ""
        prov = extract_provenance(spec)
        assert "timeframe" not in prov
        assert "holding_period" not in prov

    def test_cost_model_id_changes_when_any_knob_drifts(self) -> None:
        a = extract_provenance(_valid_spec())
        b_spec = _valid_spec()
        b_spec["cost_model"]["fee_bps"] = 0.5  # was 0.4
        b = extract_provenance(b_spec)
        assert a["cost_model_id"] != b["cost_model_id"]


# --- Round 20: load_spec_provenance one-call helper -----------------

from hft_platform.alpha.strategy_spec import load_spec_provenance


class TestLoadSpecProvenance:
    def test_returns_none_for_missing_alpha_id(self, tmp_path: Path) -> None:
        # Empty root; unknown id should resolve to None, not raise.
        assert load_spec_provenance("c99_missing", root=tmp_path) is None

    def test_resolves_alpha_id_under_root(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "c01"
        spec_dir.mkdir()
        (spec_dir / "spec.yaml").write_text(
            yaml.safe_dump(_valid_spec()),
            encoding="utf-8",
        )
        prov = load_spec_provenance("c01", root=tmp_path)
        assert prov is not None
        assert prov["data_range"] == "2026-01-02..2026-05-13"

    def test_resolves_explicit_file_path(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump(_valid_spec()), encoding="utf-8")
        prov = load_spec_provenance(path)
        assert prov is not None
        assert "shioaji_measured_p95" in prov["cost_model_id"]

    def test_repo_exemplar_resolves(self) -> None:
        # The Round-13 exemplar must produce a non-None provenance row.
        prov = load_spec_provenance("_templates", root="research/alphas")
        assert prov is not None
        assert prov["data_range"]
        assert prov["cost_model_id"]
        assert prov["required_gates"]

    def test_corrupt_yaml_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text("not: a: valid: yaml: : :", encoding="utf-8")
        assert load_spec_provenance(path) is None

    def test_non_mapping_spec_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "spec.yaml"
        path.write_text("- one\n- two\n", encoding="utf-8")
        assert load_spec_provenance(path) is None
