"""Causal (no-lookahead) PDQ_cont entry mask.

Exercises that `build_opportunity_mask_causal` computes its quantile
thresholds from strictly prior days only -- perturbing a later day's
feature values must never change an earlier day's eligibility -- and that
the warmup period produces zero eligible events, since there is not yet
enough prior history to calibrate a threshold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.tools.pdq_causal_walkforward import build_opportunity_mask_causal

N_DAYS = 20
ROWS_PER_DAY = 10
WARMUP_DAYS = 5


def _make_frame(*, last_day_extreme: bool) -> pd.DataFrame:
    rng = np.random.default_rng(20260709)
    days = [f"2026-03-{day:02d}" for day in range(1, N_DAYS + 1)]
    rows = []
    for day_index, day in enumerate(days):
        for _ in range(ROWS_PER_DAY):
            rows.append(
                {
                    "day": day,
                    "C60": rng.normal(0.0, 1.0),
                    "rvexp": rng.uniform(0.5, 1.5),
                    "spread_agg": rng.uniform(1.0, 3.0),
                    "d5_agg": rng.uniform(10.0, 100.0),
                    "cross_sync_ge2": 1,
                    "signC60": rng.choice([-1, 1]),
                }
            )
    frame = pd.DataFrame(rows)
    if last_day_extreme:
        last_day_mask = frame["day"].eq(days[-1])
        frame.loc[last_day_mask, "C60"] = 1000.0
        frame.loc[last_day_mask, "rvexp"] = 1000.0
        frame.loc[last_day_mask, "spread_agg"] = 0.001
        frame.loc[last_day_mask, "d5_agg"] = 1000.0
    return frame


def test_warmup_days_have_no_eligible_events() -> None:
    frame = _make_frame(last_day_extreme=False)
    mask = build_opportunity_mask_causal(frame, warmup_days=WARMUP_DAYS)

    warmup_days = sorted(frame["day"].unique())[:WARMUP_DAYS]
    warmup_rows = frame["day"].isin(warmup_days)

    assert not mask[warmup_rows].any()


def test_causal_mask_is_unaffected_by_a_future_day_becoming_extreme() -> None:
    baseline = _make_frame(last_day_extreme=False)
    perturbed = baseline.copy()
    last_day = sorted(baseline["day"].unique())[-1]
    last_day_mask = perturbed["day"].eq(last_day)
    perturbed.loc[last_day_mask, "C60"] = 1000.0
    perturbed.loc[last_day_mask, "rvexp"] = 1000.0
    perturbed.loc[last_day_mask, "spread_agg"] = 0.001
    perturbed.loc[last_day_mask, "d5_agg"] = 1000.0

    mask_baseline = build_opportunity_mask_causal(baseline, warmup_days=WARMUP_DAYS)
    mask_perturbed = build_opportunity_mask_causal(perturbed, warmup_days=WARMUP_DAYS)

    prior_rows = ~last_day_mask
    pd.testing.assert_series_equal(
        mask_baseline[prior_rows],
        mask_perturbed[prior_rows],
        check_names=False,
    )


def test_mask_requires_cross_sync_and_nonzero_sign() -> None:
    frame = _make_frame(last_day_extreme=False)
    last_day = sorted(frame["day"].unique())[-1]
    last_day_mask = frame["day"].eq(last_day)
    prior = frame.loc[~last_day_mask]
    # Push C60/rvexp well past their prior-days quantile, and pick a
    # spread/depth pair that is wide enough to fail the "stable book"
    # exclusion (spread above the prior median) while still clearing the
    # depth floor -- i.e. comfortably past every threshold, not borderline.
    frame.loc[last_day_mask, "C60"] = prior["C60"].abs().quantile(0.95) + 10.0
    frame.loc[last_day_mask, "rvexp"] = prior["rvexp"].quantile(0.90) + 10.0
    frame.loc[last_day_mask, "spread_agg"] = prior["spread_agg"].median() + 0.01
    frame.loc[last_day_mask, "d5_agg"] = prior["d5_agg"].quantile(0.20) + 10.0
    frame.loc[last_day_mask, "signC60"] = 1

    disqualified = frame.copy()
    disqualified.loc[last_day_mask, "cross_sync_ge2"] = 0

    zero_sign = frame.copy()
    zero_sign.loc[last_day_mask, "signC60"] = 0

    mask_eligible = build_opportunity_mask_causal(frame, warmup_days=WARMUP_DAYS)
    mask_disqualified = build_opportunity_mask_causal(disqualified, warmup_days=WARMUP_DAYS)
    mask_zero_sign = build_opportunity_mask_causal(zero_sign, warmup_days=WARMUP_DAYS)

    assert mask_eligible[last_day_mask].any()
    assert not mask_disqualified[last_day_mask].any()
    assert not mask_zero_sign[last_day_mask].any()
