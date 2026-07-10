"""Grid-size invariant for the uncapped Supertrend grid search."""

from __future__ import annotations

import itertools

from research.tools.pdq_causal_supertrend_grid_search_uncapped import (
    ATR_PERIODS,
    FACTORS,
    FIXED_LIQUIDITY_PARAMS,
    TIMEFRAMES,
)


def test_grid_has_at_least_1000_unique_combinations() -> None:
    combos = list(itertools.product(TIMEFRAMES, ATR_PERIODS, FACTORS))

    assert len(combos) == len(TIMEFRAMES) * len(ATR_PERIODS) * len(FACTORS)
    assert len(combos) >= 1000
    assert len(set(combos)) == len(combos)


def test_fixed_liquidity_params_has_no_max_hold_field() -> None:
    assert "max_hold_s" not in FIXED_LIQUIDITY_PARAMS
    assert set(FIXED_LIQUIDITY_PARAMS) == {
        "min_depth_ratio",
        "max_spread_ratio",
        "min_zlogl_delta",
        "confirmations",
    }
