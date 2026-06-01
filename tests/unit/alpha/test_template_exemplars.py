"""Round 36: pin the shape of multi-leg exemplar specs.

The exemplars under ``research/alphas/_templates/`` are the entry
ramp for goal §2 (multi-leg / Greeks).  Locking their shape under
test means future schema evolution can't silently break them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.alpha.strategy_spec import (
    classify_strategy_shape,
    has_options,
    is_multi_leg,
    load_spec,
    validate_spec,
)

TEMPLATES_DIR = Path("research/alphas/_templates")
STRADDLE_PATH = TEMPLATES_DIR / "spec.straddle.yaml"
FUTURES_PAIR_PATH = TEMPLATES_DIR / "spec.futures_pair.yaml"
SINGLE_PATH = TEMPLATES_DIR / "spec.yaml"


class TestStraddleExemplar:
    @pytest.fixture
    def spec(self) -> dict:
        return load_spec(STRADDLE_PATH)

    def test_file_exists(self) -> None:
        assert STRADDLE_PATH.is_file(), STRADDLE_PATH

    def test_validates_clean(self, spec: dict) -> None:
        errors = validate_spec(spec)
        assert errors == [], errors

    def test_is_multi_leg(self, spec: dict) -> None:
        assert is_multi_leg(spec) is True

    def test_has_options(self, spec: dict) -> None:
        assert has_options(spec) is True

    def test_classified_as_straddle(self, spec: dict) -> None:
        assert classify_strategy_shape(spec) == "straddle"

    def test_declares_greeks_exposure(self, spec: dict) -> None:
        block = spec.get("greeks_exposure")
        assert isinstance(block, dict)
        assert set(block.keys()) >= {
            "max_net_delta",
            "max_net_gamma",
            "max_net_vega",
            "max_net_theta",
        }

    def test_net_edge_floor_honors_goal_floor(self, spec: dict) -> None:
        # 限制 §3 floor must not be relaxed in the exemplar.
        floor = spec["validation_plan"]["net_edge_floor_pts"]
        assert floor >= 10.0


class TestFuturesPairExemplar:
    @pytest.fixture
    def spec(self) -> dict:
        return load_spec(FUTURES_PAIR_PATH)

    def test_file_exists(self) -> None:
        assert FUTURES_PAIR_PATH.is_file(), FUTURES_PAIR_PATH

    def test_validates_clean(self, spec: dict) -> None:
        errors = validate_spec(spec)
        assert errors == [], errors

    def test_is_multi_leg(self, spec: dict) -> None:
        assert is_multi_leg(spec) is True

    def test_has_no_options(self, spec: dict) -> None:
        assert has_options(spec) is False

    def test_classified_as_multi_leg_futures(self, spec: dict) -> None:
        assert classify_strategy_shape(spec) == "multi_leg_futures"

    def test_no_greeks_exposure_block(self, spec: dict) -> None:
        # No option legs -> greeks_exposure intentionally absent so
        # the example doesn't teach authors to copy a dummy block.
        assert "greeks_exposure" not in spec

    def test_legs_carry_required_fields(self, spec: dict) -> None:
        legs = spec["legs"]
        assert len(legs) == 2
        for leg in legs:
            assert leg["symbol"]
            assert leg["side"] in {"long", "short"}
            assert isinstance(leg["qty"], int) and leg["qty"] > 0

    def test_net_edge_floor_honors_goal_floor(self, spec: dict) -> None:
        floor = spec["validation_plan"]["net_edge_floor_pts"]
        assert floor >= 10.0


class TestSingleLegExemplarUnchanged:
    """Round 36 must NOT regress the single-leg exemplar (Round 13)."""

    def test_single_exemplar_still_classifies_as_single(self) -> None:
        spec = load_spec(SINGLE_PATH)
        assert classify_strategy_shape(spec) == "single"
        assert is_multi_leg(spec) is False
        assert has_options(spec) is False


class TestExemplarsAreDistinct:
    def test_three_distinct_strategy_names(self) -> None:
        names = {
            load_spec(SINGLE_PATH)["strategy_name"],
            load_spec(STRADDLE_PATH)["strategy_name"],
            load_spec(FUTURES_PAIR_PATH)["strategy_name"],
        }
        assert len(names) == 3, names

    def test_each_exemplar_covers_a_distinct_shape(self) -> None:
        shapes = {
            classify_strategy_shape(load_spec(SINGLE_PATH)),
            classify_strategy_shape(load_spec(STRADDLE_PATH)),
            classify_strategy_shape(load_spec(FUTURES_PAIR_PATH)),
        }
        assert shapes == {"single", "straddle", "multi_leg_futures"}
