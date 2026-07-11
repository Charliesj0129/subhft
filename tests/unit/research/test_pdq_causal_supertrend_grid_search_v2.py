"""Grid construction and the MAX_HOLDS-monkeypatch correctness fix for the v2 search."""

from __future__ import annotations

from research.tools.pdq_causal_supertrend_grid_search_v2 import (
    ATR_PERIODS,
    FACTORS,
    HOLD_VALUES,
    TIMEFRAMES,
    build_grid,
    exit_search,
)


def test_build_grid_has_at_least_1000_unique_combinations() -> None:
    genes = build_grid()

    assert len(genes) == len(TIMEFRAMES) * len(ATR_PERIODS) * len(FACTORS) * len(HOLD_VALUES)
    assert len(genes) >= 1000
    assert len(set(genes)) == len(genes)
    assert {gene.max_hold_s for gene in genes} == set(HOLD_VALUES)


def test_exit_search_max_holds_is_monkeypatched_to_include_every_tested_hold_value() -> None:
    """Regression guard: the internal Supertrend/liquidity scan caps at max(MAX_HOLDS).

    If this module failed to widen `exit_search.MAX_HOLDS` before constructing
    `ExitEvaluator`, a max_hold_s=3600 gene would silently miss any exit signal
    between the old cap (1800s) and 3600s -- not raise an error, just produce a
    wrong answer. This test only checks the module attribute (cheap, no data
    load); the actual exit-timing behavior is covered by the existing
    `armed_flip_exit_times` / `liquidity_exit_times_for_events` tests.
    """
    assert exit_search.MAX_HOLDS == HOLD_VALUES
    assert max(exit_search.MAX_HOLDS) == 3600
