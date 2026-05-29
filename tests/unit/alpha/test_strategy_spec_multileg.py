"""Round 35: ``legs:`` + ``greeks_exposure:`` schema (goal §2).

Multi-leg / Greeks entry point — schema and validator only, engine
untouched.  Locks the shape so spread / straddle / strangle / calendar
candidates can be authored before the matching execution path lands.
"""

from __future__ import annotations

import copy

import pytest

from hft_platform.alpha.strategy_spec import (
    classify_strategy_shape,
    has_options,
    is_multi_leg,
    validate_spec,
)


def _baseline() -> dict:
    """Single-leg baseline; legs / greeks_exposure both absent (legal)."""
    return {
        "strategy_name": "c99_demo",
        "market": "TAIFEX",
        "instrument": "TXFD6",
        "hypothesis": "Opening-range expansion drives momentum.",
        "timeframe": "5m",
        "holding_period": "intraday <2h",
        "frequency_class": "intraday_hft",
        "entry_rule": "Break of opening range high after 09:00",
        "exit_rule": "5m close back below range OR 13:25 force flat",
        "position_sizing": "fixed 1 lot",
        "risk_control": {
            "max_position": 1,
            "max_drawdown_pts": 80,
            "force_flat_rule": "13:25 TPE close",
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
            "sample_targets": {"min_round_trips": 300, "min_oos_trading_days": 60},
            "required_gates": ["min_sample_size", "edge_per_round_trip"],
            "net_edge_floor_pts": 10.0,
        },
    }


def _futures_pair_legs() -> list[dict]:
    return [
        {"symbol": "TXFD6", "side": "long", "qty": 1},
        {"symbol": "TMFD6", "side": "short", "qty": 4},
    ]


def _straddle_legs(*, strike: float = 18000.0, expiry: str = "2026-06-18") -> list[dict]:
    return [
        {
            "symbol": "TXO_C",
            "side": "long",
            "qty": 1,
            "option": {"right": "C", "strike": strike, "expiry": expiry},
        },
        {
            "symbol": "TXO_P",
            "side": "long",
            "qty": 1,
            "option": {"right": "P", "strike": strike, "expiry": expiry},
        },
    ]


def _greeks_block() -> dict:
    return {
        "max_net_delta": 1.0,
        "max_net_gamma": 0.05,
        "max_net_vega": 200.0,
        "max_net_theta": -50.0,
    }


class TestLegsAbsent:
    def test_baseline_without_legs_passes(self) -> None:
        assert validate_spec(_baseline()) == []

    def test_is_multi_leg_false_for_single_string_instrument(self) -> None:
        assert is_multi_leg(_baseline()) is False

    def test_classify_returns_single_when_no_legs(self) -> None:
        assert classify_strategy_shape(_baseline()) == "single"


class TestLegsShape:
    def test_futures_pair_legs_valid(self) -> None:
        spec = _baseline()
        spec["legs"] = _futures_pair_legs()
        assert validate_spec(spec) == []
        assert is_multi_leg(spec) is True
        assert has_options(spec) is False
        assert classify_strategy_shape(spec) == "multi_leg_futures"

    def test_legs_must_be_list(self) -> None:
        spec = _baseline()
        spec["legs"] = {"a": 1}
        errors = validate_spec(spec)
        assert any("legs must be a list" in e for e in errors)

    def test_single_element_legs_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = [_futures_pair_legs()[0]]
        errors = validate_spec(spec)
        assert any("length<2" in e for e in errors)

    @pytest.mark.parametrize("field", ["symbol", "side", "qty"])
    def test_each_leg_field_required(self, field: str) -> None:
        spec = _baseline()
        spec["legs"] = _futures_pair_legs()
        spec["legs"][0].pop(field)
        errors = validate_spec(spec)
        assert any(f"legs[0].{field}" in e for e in errors)

    def test_unknown_side_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = _futures_pair_legs()
        spec["legs"][0]["side"] = "flat"
        errors = validate_spec(spec)
        assert any("legs[0].side" in e for e in errors)

    def test_non_positive_qty_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = _futures_pair_legs()
        spec["legs"][0]["qty"] = 0
        errors = validate_spec(spec)
        assert any("legs[0].qty" in e for e in errors)

    def test_bool_qty_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = _futures_pair_legs()
        spec["legs"][0]["qty"] = True  # type: ignore[typeddict-item]
        errors = validate_spec(spec)
        assert any("legs[0].qty" in e for e in errors)

    def test_non_dict_leg_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = ["TXFD6", "TMFD6"]
        errors = validate_spec(spec)
        assert any("legs[0] must be a mapping" in e for e in errors)


class TestOptionLegs:
    def test_straddle_with_greeks_passes(self) -> None:
        spec = _baseline()
        spec["legs"] = _straddle_legs()
        spec["greeks_exposure"] = _greeks_block()
        assert validate_spec(spec) == []
        assert has_options(spec) is True
        assert classify_strategy_shape(spec) == "straddle"

    def test_option_legs_without_greeks_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = _straddle_legs()
        errors = validate_spec(spec)
        assert any("greeks_exposure" in e for e in errors)

    @pytest.mark.parametrize("field", ["right", "strike", "expiry"])
    def test_each_option_field_required(self, field: str) -> None:
        spec = _baseline()
        spec["legs"] = _straddle_legs()
        spec["greeks_exposure"] = _greeks_block()
        spec["legs"][0]["option"].pop(field)
        errors = validate_spec(spec)
        assert any(f"legs[0].option.{field}" in e for e in errors)

    def test_unknown_right_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = _straddle_legs()
        spec["greeks_exposure"] = _greeks_block()
        spec["legs"][0]["option"]["right"] = "X"
        errors = validate_spec(spec)
        assert any("legs[0].option.right" in e for e in errors)

    def test_non_numeric_strike_rejected(self) -> None:
        spec = _baseline()
        spec["legs"] = _straddle_legs()
        spec["greeks_exposure"] = _greeks_block()
        spec["legs"][0]["option"]["strike"] = "ATM"
        errors = validate_spec(spec)
        assert any("legs[0].option.strike" in e for e in errors)


class TestGreeksExposure:
    def test_must_be_mapping(self) -> None:
        spec = _baseline()
        spec["greeks_exposure"] = [1, 2, 3]
        errors = validate_spec(spec)
        assert any("greeks_exposure must be a mapping" in e for e in errors)

    def test_empty_rejected(self) -> None:
        spec = _baseline()
        spec["greeks_exposure"] = {}
        errors = validate_spec(spec)
        assert any("greeks_exposure: empty" in e for e in errors)

    def test_non_numeric_value_rejected(self) -> None:
        spec = _baseline()
        spec["greeks_exposure"] = {"max_net_delta": "tight"}
        errors = validate_spec(spec)
        assert any("max_net_delta" in e for e in errors)

    def test_bool_value_rejected(self) -> None:
        spec = _baseline()
        spec["greeks_exposure"] = {"max_net_vega": True}
        errors = validate_spec(spec)
        assert any("max_net_vega" in e for e in errors)

    def test_optional_when_no_options(self) -> None:
        spec = _baseline()
        spec["legs"] = _futures_pair_legs()
        # No greeks_exposure: legal because no leg carries option payload.
        assert validate_spec(spec) == []


class TestClassifyShape:
    def test_strangle_two_strikes_same_expiry(self) -> None:
        spec = _baseline()
        legs = _straddle_legs()
        legs[1]["option"]["strike"] = 18500.0  # call=18000, put=18500 -> strangle
        spec["legs"] = legs
        spec["greeks_exposure"] = _greeks_block()
        assert classify_strategy_shape(spec) == "strangle"

    def test_calendar_same_strike_different_expiry(self) -> None:
        spec = _baseline()
        legs = [
            {
                "symbol": "TXO_C_FRONT",
                "side": "short",
                "qty": 1,
                "option": {"right": "C", "strike": 18000.0, "expiry": "2026-06-18"},
            },
            {
                "symbol": "TXO_C_BACK",
                "side": "long",
                "qty": 1,
                "option": {"right": "C", "strike": 18000.0, "expiry": "2026-07-16"},
            },
        ]
        spec["legs"] = legs
        spec["greeks_exposure"] = _greeks_block()
        assert classify_strategy_shape(spec) == "calendar"

    def test_vertical_spread_same_right_two_strikes(self) -> None:
        spec = _baseline()
        legs = [
            {
                "symbol": "TXO_C_LO",
                "side": "long",
                "qty": 1,
                "option": {"right": "C", "strike": 18000.0, "expiry": "2026-06-18"},
            },
            {
                "symbol": "TXO_C_HI",
                "side": "short",
                "qty": 1,
                "option": {"right": "C", "strike": 18200.0, "expiry": "2026-06-18"},
            },
        ]
        spec["legs"] = legs
        spec["greeks_exposure"] = _greeks_block()
        assert classify_strategy_shape(spec) == "vertical_spread"

    def test_mixed_futures_and_options_classified_as_options_multi(self) -> None:
        spec = _baseline()
        legs = [
            {"symbol": "TXFD6", "side": "long", "qty": 1},
            {
                "symbol": "TXO_C",
                "side": "short",
                "qty": 1,
                "option": {"right": "C", "strike": 18000.0, "expiry": "2026-06-18"},
            },
        ]
        spec["legs"] = legs
        spec["greeks_exposure"] = _greeks_block()
        assert classify_strategy_shape(spec) == "options_multi"


class TestDefensive:
    def test_does_not_mutate_input(self) -> None:
        spec = _baseline()
        spec["legs"] = _straddle_legs()
        spec["greeks_exposure"] = _greeks_block()
        before = copy.deepcopy(spec)
        validate_spec(spec)
        assert spec == before
