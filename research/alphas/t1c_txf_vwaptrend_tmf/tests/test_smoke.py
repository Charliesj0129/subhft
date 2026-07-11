"""Smoke checks for the T1-C governed research package."""

from __future__ import annotations

from hft_platform.alpha.strategy_spec import load_spec, validate_spec
from research.alphas.t1c_txf_vwaptrend_tmf.impl import (
    ALPHA_ID,
    SPEC_PATH,
    is_promotion_eligible_v0,
)


def test_spec_is_valid_and_not_promotion_eligible() -> None:
    spec = load_spec(SPEC_PATH)

    assert validate_spec(spec) == []
    assert spec["strategy_name"] == ALPHA_ID
    assert spec["validation_plan"]["net_edge_floor_pts"] >= 10.0
    assert "edge_per_round_trip" in spec["validation_plan"]["required_gates"]
    assert "replay_parity" in spec["validation_plan"]["required_gates"]
    assert is_promotion_eligible_v0() is False
