"""Correctness tests for the new opportunity-layer isolation machinery.

These target mathematical/mechanical properties of the pieces that are new
in this script (day-local circular-shift permutation null, onset detection,
day-end-bounded fixed exit, and the parameterized causal opportunity mask).
The grid/PBO/DSR machinery reused from `pdq_supertrend_signal_lopez_
validation.py` is already tested there; it is not re-tested here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.tools.pdq_opportunity_standalone_validation import (
    build_opportunity_mask_param,
    compute_agree,
    day_local_circular_shift,
    exit_at_horizon_or_day_end,
    onset_mask,
    permutation_test_horizon,
    precompute_day_layout,
)


def test_onset_mask_flags_only_first_second_of_each_run() -> None:
    mask = np.array([False, True, True, False, True, False, True, True])
    day_code = np.array([0, 0, 0, 0, 0, 1, 1, 1])  # day boundary between idx 4 and 5

    onset = onset_mask(mask, day_code)

    assert list(onset) == [False, True, False, False, True, False, True, False]


def test_onset_mask_treats_day_boundary_as_a_new_run_even_if_both_sides_true() -> None:
    mask = np.array([True, True, True])
    day_code = np.array([0, 1, 1])  # a run that is "True" straddling a day change

    onset = onset_mask(mask, day_code)

    assert list(onset) == [True, True, False]


def test_precompute_day_layout_local_pos_resets_at_each_day() -> None:
    day_code = np.array([0, 0, 0, 1, 1, 2, 2, 2, 2])

    day_start, day_len, compact_rank, local_pos = precompute_day_layout(day_code)

    assert list(day_start) == [0, 3, 5]
    assert list(day_len) == [3, 2, 4]
    assert list(compact_rank) == [0, 0, 0, 1, 1, 2, 2, 2, 2]
    assert list(local_pos) == [0, 1, 2, 0, 1, 0, 1, 2, 3]


def test_day_local_circular_shift_preserves_per_day_true_count() -> None:
    rng = np.random.default_rng(0)
    day_code = np.repeat(np.arange(10), 37)
    mask = rng.random(len(day_code)) > 0.7
    day_start, day_len, compact_rank, local_pos = precompute_day_layout(day_code)

    shifted = day_local_circular_shift(mask, day_start, day_len, compact_rank, local_pos, rng)

    mask_df = pd.DataFrame({"day": day_code, "mask": mask})
    shifted_df = pd.DataFrame({"day": day_code, "mask": shifted})
    assert (mask_df.groupby("day")["mask"].sum() == shifted_df.groupby("day")["mask"].sum()).all()


def test_day_local_circular_shift_actually_moves_events_within_a_day() -> None:
    day_code = np.zeros(50, dtype=int)
    mask = np.zeros(50, dtype=bool)
    mask[5] = True
    day_start, day_len, compact_rank, local_pos = precompute_day_layout(day_code)

    seen_positions = set()
    for seed in range(20):
        rng = np.random.default_rng(seed)
        shifted = day_local_circular_shift(mask, day_start, day_len, compact_rank, local_pos, rng)
        seen_positions.add(int(np.flatnonzero(shifted)[0]))

    assert len(seen_positions) > 1


def test_permutation_test_horizon_detects_a_planted_signal() -> None:
    rng = np.random.default_rng(1)
    n_days, per_day = 30, 200
    day_code = np.repeat(np.arange(n_days), per_day)
    day_start, day_len, compact_rank, local_pos = precompute_day_layout(day_code)

    onset = rng.random(len(day_code)) > 0.95
    valid = np.ones(len(day_code), dtype=bool)
    move = rng.normal(loc=1.0, scale=1.0, size=len(day_code))
    move[onset] += 6.0  # strong planted effect at onset seconds

    result = permutation_test_horizon(
        onset, valid, move, day_start, day_len, compact_rank, local_pos, n_perm=300, rng=rng
    )

    assert result["observed_gap"] > 3.0
    assert result["p_value"] < 0.01


def test_permutation_test_horizon_pure_noise_is_not_degenerate() -> None:
    rng = np.random.default_rng(2)
    n_days, per_day = 30, 200
    day_code = np.repeat(np.arange(n_days), per_day)
    day_start, day_len, compact_rank, local_pos = precompute_day_layout(day_code)

    onset = rng.random(len(day_code)) > 0.95
    valid = np.ones(len(day_code), dtype=bool)
    move = rng.normal(loc=1.0, scale=1.0, size=len(day_code))  # no relationship to onset

    result = permutation_test_horizon(
        onset, valid, move, day_start, day_len, compact_rank, local_pos, n_perm=300, rng=rng
    )

    assert 0.05 <= result["p_value"] <= 0.95


def test_exit_at_horizon_or_day_end_uses_day_end_when_horizon_exceeds_it() -> None:
    # 4 bars/day at 5s cadence -> day ends at step index 3 (15s after entry).
    future = np.array([[100.0, 101.0, 102.0, 103.0, np.nan, np.nan, np.nan]])
    same = np.array([[True, True, True, True, False, False, False]])

    exit_price, hold_actual = exit_at_horizon_or_day_end(future, same, idx=np.array([0]), step=6)

    assert exit_price[0] == 103.0
    assert hold_actual[0] == 15.0


def test_exit_at_horizon_or_day_end_uses_horizon_when_within_day() -> None:
    future = np.array([[100.0, 101.0, 102.0, 103.0, 104.0]])
    same = np.array([[True, True, True, True, True]])

    exit_price, hold_actual = exit_at_horizon_or_day_end(future, same, idx=np.array([0]), step=3)

    assert exit_price[0] == 103.0
    assert hold_actual[0] == 15.0


def _synthetic_pdq_frame() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    n_days, per_day = 20, 100
    n = n_days * per_day
    day = np.repeat([f"2026-03-{d + 1:02d}" for d in range(n_days)], per_day)
    c60 = rng.normal(scale=1.0, size=n)
    return pd.DataFrame(
        {
            "day": day,
            "C60": c60,
            "rvexp": rng.uniform(0.5, 2.0, size=n),
            "spread_agg": rng.uniform(1.0, 3.0, size=n),
            "d5_agg": rng.uniform(50.0, 200.0, size=n),
            "signC60": np.sign(c60).astype(np.int8),
            "nimp60_TXF": c60 + rng.normal(scale=0.3, size=n),
            "nimp60_MXF": c60 + rng.normal(scale=0.3, size=n),
            "nimp60_TMF": rng.normal(scale=1.0, size=n),  # mostly independent of C60
        }
    )


def test_compute_agree_counts_roots_matching_signc60_sign() -> None:
    df = pd.DataFrame(
        {
            "signC60": np.array([1, -1, 1], dtype=np.int8),
            "nimp60_TXF": [1.0, -1.0, 1.0],
            "nimp60_MXF": [1.0, 1.0, -1.0],
            "nimp60_TMF": [-1.0, -1.0, 1.0],
        }
    )

    agree = compute_agree(df)

    assert list(agree) == [2, 2, 2]


def test_build_opportunity_mask_param_is_monotonically_stricter_with_higher_cross_sync_min() -> None:
    df = _synthetic_pdq_frame()
    agree = compute_agree(df)

    mask_min1 = build_opportunity_mask_param(df, agree, c_q=0.95, rv_q=0.90, cross_sync_min=1)
    mask_min2 = build_opportunity_mask_param(df, agree, c_q=0.95, rv_q=0.90, cross_sync_min=2)
    mask_min3 = build_opportunity_mask_param(df, agree, c_q=0.95, rv_q=0.90, cross_sync_min=3)

    assert mask_min1.sum() >= mask_min2.sum() >= mask_min3.sum()
    # a stricter cross-sync requirement can only remove candidates, never add.
    assert ((mask_min2) & ~(mask_min1)).sum() == 0
    assert ((mask_min3) & ~(mask_min2)).sum() == 0


def test_build_opportunity_mask_param_is_monotonically_stricter_with_higher_c_quantile() -> None:
    df = _synthetic_pdq_frame()
    agree = compute_agree(df)

    mask_loose = build_opportunity_mask_param(df, agree, c_q=0.90, rv_q=0.90, cross_sync_min=1)
    mask_strict = build_opportunity_mask_param(df, agree, c_q=0.99, rv_q=0.90, cross_sync_min=1)

    assert mask_strict.sum() <= mask_loose.sum()
