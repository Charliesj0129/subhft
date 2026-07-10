from __future__ import annotations

import numpy as np
import pandas as pd

from research.tools.pdq_supertrend_exit_search import (
    Gene,
    armed_flip_exit_times,
    build_completed_bars,
    canonical_gene,
    choose_exit_index,
    complete_event_mask,
    compute_supertrend_direction,
    execution_indices_for_times,
    first_armed_flip_index,
    first_liquidity_recovery_index,
    liquidity_exit_times_for_events,
    positive_rate,
    same_band_value,
)


def test_armed_flip_waits_for_alignment_before_exiting() -> None:
    states = np.array([-1, -1, 1, 1, -1], dtype=np.int8)

    assert first_armed_flip_index(states, position_dir=1) == 4


def test_armed_flip_exits_on_first_confirmed_opposite_state() -> None:
    states = np.array([1, 1, -1, -1], dtype=np.int8)

    assert first_armed_flip_index(states, position_dir=1) == 2


def test_armed_flip_does_not_exit_before_it_has_armed() -> None:
    states = np.array([-1, -1, -1], dtype=np.int8)

    assert first_armed_flip_index(states, position_dir=1) is None


def test_liquidity_recovery_requires_consecutive_causal_confirmations() -> None:
    depth = np.array([100.0, 125.0, 130.0])
    spread = np.array([4.0, 3.0, 3.0])
    zlogl = np.array([-2.0, -1.2, -1.0])

    result = first_liquidity_recovery_index(
        depth,
        spread,
        zlogl,
        min_depth_ratio=1.2,
        max_spread_ratio=0.8,
        min_zlogl_delta=0.5,
        confirmations=2,
    )

    assert result == 2


def test_first_of_two_exit_uses_earliest_available_signal() -> None:
    assert choose_exit_index(supertrend_index=5, liquidity_index=3, max_index=9) == (3, "liquidity")
    assert choose_exit_index(supertrend_index=5, liquidity_index=None, max_index=9) == (5, "supertrend")
    assert choose_exit_index(supertrend_index=None, liquidity_index=None, max_index=9) == (9, "max_hold")


def test_supertrend_direction_finishes_in_uptrend_for_rising_prices() -> None:
    close = np.arange(100.0, 130.0)
    high = close + 1.0
    low = close - 1.0

    direction = compute_supertrend_direction(high, low, close, atr_period=3, factor=2.0)

    assert direction[-1] == 1
    assert set(direction[~np.isnan(direction)].astype(int)) <= {-1, 1}


def test_armed_flip_uses_only_bars_completed_after_entry() -> None:
    bar_end_s = np.array([60, 120, 180, 240], dtype=np.int64)
    states = np.array([1, -1, -1, 1], dtype=np.int8)
    entry_s = np.array([75], dtype=np.int64)
    position_dirs = np.array([1], dtype=np.int8)

    exits = armed_flip_exit_times(
        bar_end_s,
        states,
        entry_s,
        position_dirs,
        max_hold_s=300,
    )

    assert exits.tolist() == [120]


def test_positive_rate_excludes_missing_paths_from_denominator() -> None:
    values = pd.Series([1.0, -1.0, np.nan])

    assert positive_rate(values) == 0.5


def test_execution_index_rejects_price_after_timestamp_gap() -> None:
    seconds = np.array([0, 100], dtype=np.int64)

    result = execution_indices_for_times(
        seconds,
        np.array([20], dtype=np.int64),
        max_lag_s=5,
    )

    assert result.tolist() == [-1]


def test_complete_event_mask_uses_common_longest_horizon() -> None:
    seconds = np.array([0, 300, 1800, 2000], dtype=np.int64)

    result = complete_event_mask(
        seconds,
        np.array([0, 100], dtype=np.int64),
        max_hold_s=1800,
        max_lag_s=5,
    )

    assert result.tolist() == [True, False]


def test_liquidity_confirmation_resets_across_timestamp_gap() -> None:
    seconds = np.array([0, 1, 100, 101], dtype=np.int64)
    depth = np.array([100.0, 130.0, 130.0, 130.0])
    spread = np.array([4.0, 3.0, 3.0, 3.0])
    zlogl = np.array([-2.0, -1.0, -1.0, -1.0])

    exits = liquidity_exit_times_for_events(
        seconds,
        depth,
        spread,
        zlogl,
        entry_indices=np.array([0], dtype=np.int64),
        entry_s=np.array([0], dtype=np.int64),
        max_hold_s=200,
        min_depth_ratio=1.2,
        max_spread_ratio=0.8,
        min_zlogl_delta=0.5,
        confirmations=2,
        max_observation_gap_s=5,
    )

    assert exits.tolist() == [101]


def test_supertrend_band_identity_uses_exact_equality() -> None:
    upper = 100.0

    assert same_band_value(upper, upper)
    assert not same_band_value(np.nextafter(upper, np.inf), upper)


def test_armed_flip_skips_unexecutable_flip_and_waits_for_rearm() -> None:
    exits = armed_flip_exit_times(
        bar_end_s=np.array([60, 120, 180, 240], dtype=np.int64),
        states=np.array([1, -1, 1, -1], dtype=np.int8),
        entry_s=np.array([75], dtype=np.int64),
        position_dirs=np.array([1], dtype=np.int8),
        max_hold_s=300,
        execution_seconds=np.array([60, 180, 240], dtype=np.int64),
        max_execution_lag_s=5,
    )

    assert exits.tolist() == [240]


def test_completed_bars_exclude_partial_bucket_before_session_gap() -> None:
    frame = pd.DataFrame(
        {
            "sec": [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 1000, 1001],
            "mid_agg": np.arange(14, dtype=float),
        }
    )

    bars = build_completed_bars(frame, timeframe_s=60)

    assert bars["bar_end_s"].tolist() == [60]


def test_canonical_gene_removes_exit_mode_irrelevant_parameters() -> None:
    gene = Gene("15m", 60, 8.0, 300, "liquidity", 1.3, 0.7, 1.0, 5)

    canonical = canonical_gene(gene)

    assert (canonical.timeframe, canonical.atr_period, canonical.factor) == ("1m", 10, 3.0)
