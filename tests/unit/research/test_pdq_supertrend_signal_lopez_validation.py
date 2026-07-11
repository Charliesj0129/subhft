"""Correctness tests for the Lopez de Prado PBO/CSCV and DSR statistics.

These are the load-bearing new statistical machinery in this script (the
grid/trade-building plumbing reuses already-tested exit_search primitives),
so tests target their actual mathematical properties on synthetic data
rather than the full evaluator pipeline (which needs the real dataset and is
already exercised end-to-end by running the script directly).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.tools.pdq_supertrend_signal_lopez_validation import (
    ATR_PERIODS,
    FACTORS,
    TIMEFRAMES,
    cscv_pbo,
    daily_net_series,
    deflated_sharpe_ratio,
    expected_max_sharpe_under_null,
    grid,
)


def test_grid_has_1014_unique_combinations_matching_established_thread_grid() -> None:
    combos = grid()

    assert len(combos) == len(TIMEFRAMES) * len(ATR_PERIODS) * len(FACTORS)
    assert len(combos) == 1014
    assert len(set(combos)) == len(combos)


def test_cscv_pbo_returns_correct_number_of_paths_for_n_groups() -> None:
    rng = np.random.default_rng(0)
    matrix = rng.normal(size=(60, 10))

    result = cscv_pbo(matrix, n_groups=6)

    assert result["n_paths"] == 20  # C(6, 3)
    assert result["n_groups"] == 6


def test_cscv_pbo_is_zero_when_one_trial_dominates_every_split() -> None:
    """A trial with a real, stable edge should win every IS fold and also
    rank at the top OOS in every fold -- PBO must be exactly 0 in that case.
    """
    t_periods = 60
    matrix = np.zeros((t_periods, 5))
    matrix[:, 0] = np.tile([9.0, 11.0], t_periods // 2)  # mean 10, small stable variance

    result = cscv_pbo(matrix, n_groups=6)

    assert result["pbo"] == 0.0


def test_cscv_pbo_pure_noise_is_not_degenerate() -> None:
    """With no real edge anywhere, PBO should land in a broad, non-extreme
    band -- not collapse to 0 (which would indicate the statistic always
    trivially favors whichever trial happens to look best) or fail to run.
    """
    rng = np.random.default_rng(42)
    matrix = rng.normal(size=(120, 30))

    result = cscv_pbo(matrix, n_groups=6)

    assert 0.15 <= result["pbo"] <= 0.85


def test_expected_max_sharpe_under_null_increases_with_more_trials() -> None:
    small = np.array([1.0, -1.0] * 5)  # N=10, sample std ~1.05
    large = np.array([1.0, -1.0] * 500)  # N=1000, sample std ~1.0005 -- same order of magnitude

    small_benchmark = expected_max_sharpe_under_null(small)
    large_benchmark = expected_max_sharpe_under_null(large)

    assert large_benchmark > small_benchmark


def test_expected_max_sharpe_under_null_is_zero_for_single_trial() -> None:
    assert expected_max_sharpe_under_null(np.array([1.0])) == 0.0


def test_deflated_sharpe_ratio_returns_probability_in_unit_interval() -> None:
    rng = np.random.default_rng(7)
    selected = rng.normal(loc=0.05, scale=1.0, size=58)
    trial_sharpes = rng.normal(size=1014)

    result = deflated_sharpe_ratio(selected, trial_sharpes)

    assert 0.0 <= result["dsr"] <= 1.0


def test_deflated_sharpe_ratio_decreases_with_more_trials_searched() -> None:
    """Deflation must penalize a wider search: holding the winning trial's
    own track record fixed, reporting it after searching more trials (same
    cross-trial Sharpe dispersion) can only lower or hold DSR, never raise it.
    """
    rng = np.random.default_rng(11)
    selected = rng.normal(loc=0.08, scale=1.0, size=58)
    base_trial_sharpes = rng.normal(size=100)
    wider_trial_sharpes = np.tile(base_trial_sharpes, 10)  # same dispersion, 10x the trial count

    small_n_result = deflated_sharpe_ratio(selected, base_trial_sharpes)
    large_n_result = deflated_sharpe_ratio(selected, wider_trial_sharpes)

    assert large_n_result["dsr"] <= small_n_result["dsr"]


def test_daily_net_series_reindexes_missing_days_to_zero() -> None:
    trades = pd.DataFrame(
        {
            "day": ["2026-03-01", "2026-03-01", "2026-03-03"],
            "gross_pnl": [10.0, 6.0, 20.0],
        }
    )
    all_days = ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04"]

    series = daily_net_series(trades, all_days)

    assert list(series.index) == all_days
    assert series["2026-03-01"] == (10.0 - 4.0) + (6.0 - 4.0)
    assert series["2026-03-02"] == 0.0
    assert series["2026-03-03"] == 20.0 - 4.0
    assert series["2026-03-04"] == 0.0
