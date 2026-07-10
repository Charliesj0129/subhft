"""Grid construction and split-aggregation correctness for the Supertrend grid search."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.tools.pdq_causal_supertrend_grid_search import (
    ATR_PERIODS,
    FACTORS,
    FIXED_LIQUIDITY_PARAMS,
    MAX_HOLDS,
    TIMEFRAMES,
    build_grid,
    summarize_split,
)


def test_build_grid_has_at_least_1000_unique_combinations_with_fixed_liquidity_params() -> None:
    genes = build_grid()

    assert len(genes) == len(TIMEFRAMES) * len(ATR_PERIODS) * len(FACTORS) * len(MAX_HOLDS)
    assert len(genes) >= 1000
    assert len(set(genes)) == len(genes)
    for gene in genes:
        assert gene.exit_mode == FIXED_LIQUIDITY_PARAMS["exit_mode"]
        assert gene.min_depth_ratio == FIXED_LIQUIDITY_PARAMS["min_depth_ratio"]
        assert gene.max_spread_ratio == FIXED_LIQUIDITY_PARAMS["max_spread_ratio"]
        assert gene.min_zlogl_delta == FIXED_LIQUIDITY_PARAMS["min_zlogl_delta"]
        assert gene.confirmations == FIXED_LIQUIDITY_PARAMS["confirmations"]


def test_summarize_split_computes_net_cost_and_ignores_incomplete_paths() -> None:
    paths = pd.DataFrame(
        {
            "day": ["2026-03-01", "2026-03-01", "2026-03-02", "2026-06-01"],
            "gross_pnl": [10.0, -2.0, np.nan, 100.0],
            "hold_s": [300.0, 600.0, np.nan, 900.0],
        }
    )
    is_mask = np.array([True, True, True, False])

    summary = summarize_split(paths, is_mask)

    assert summary["n"] == 2
    assert summary["active_days"] == 1
    assert summary["gross_mean"] == 4.0
    assert summary["hit_rate"] == 0.5
    assert summary["net_mean_cost4"] == 0.0


def test_summarize_split_returns_nan_for_empty_mask() -> None:
    paths = pd.DataFrame({"day": ["2026-03-01"], "gross_pnl": [5.0], "hold_s": [300.0]})
    empty_mask = np.array([False])

    summary = summarize_split(paths, empty_mask)

    assert summary["n"] == 0
    assert summary["active_days"] == 0
    assert np.isnan(summary["gross_mean"])
    assert np.isnan(summary["net_mean_cost4"])
