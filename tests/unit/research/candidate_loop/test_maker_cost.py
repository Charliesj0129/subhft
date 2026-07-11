"""Maker cost view (taifex_maker_qhat_v1): hour convention, bounds, fallback."""

from __future__ import annotations

import numpy as np
import pytest

from research.backtest.calibrate_queue_fill import _hour_of_day
from research.backtest.q_hat_table import QHatTable
from research.candidate_loop.maker_cost import (
    MAKER_COST_ASSUMPTION_VERSION,
    compute_maker_cost,
    fill_probs_for_flips,
    hour_of_day_utc,
)

COST_PER_SIDE = 1.5  # comm 0.3 + tax 1.2 (TXF profile)
SPREAD = 1.0


def _table(cells: dict[tuple[str, int, str], float] | None = None) -> QHatTable:
    return QHatTable(_data=cells or {})


def _result(table: QHatTable, ts: list[int], depth: list[float], gross: float = 3.0):
    return compute_maker_cost(
        flip_ts_ns=np.asarray(ts, dtype=np.int64),
        near_side_l1_qty=np.asarray(depth, dtype=np.float64),
        gross_pts_per_flip=gross,
        median_spread_pts=SPREAD,
        cost_per_side_pts=COST_PER_SIDE,
        q_hat=table,
        q_hat_symbol="TXFD6",
    )


class TestHourConvention:
    @pytest.mark.parametrize(
        "ts_ns",
        [
            0,
            1_736_900_000_000_000_000,  # 2025-01 era
            1_775_000_000_123_456_789,  # 2026-04 era, sub-second noise
            3_599_999_999_999,  # just under one hour
        ],
    )
    def test_matches_calibration_harness_exactly(self, ts_ns: int) -> None:
        # The q_hat table is keyed by calibrate_queue_fill's UTC epoch-modulo
        # hour; any divergence silently swaps day/night liquidity regimes.
        assert hour_of_day_utc(ts_ns) == _hour_of_day(ts_ns)


class TestLookup:
    def test_calibrated_cell_used_per_flip(self) -> None:
        hour = hour_of_day_utc(1_000_000_000)
        table = _table({("TXFD6", hour, "shallow"): 0.2, ("TXFD6", hour, "deep"): 0.1})
        probs = fill_probs_for_flips(
            np.asarray([1_000_000_000, 1_000_000_000], dtype=np.int64),
            np.asarray([2.0, 9.0]),
            table,
            "TXFD6",
        )
        np.testing.assert_allclose(probs, [0.2, 0.1])  # depth<5 shallow, >=5 deep

    def test_missing_cell_falls_back_to_default(self) -> None:
        probs = fill_probs_for_flips(np.asarray([0], dtype=np.int64), np.asarray([3.0]), _table(), "TXFD6")
        np.testing.assert_allclose(probs, [0.5])

    def test_nan_depth_treated_as_zero_depth(self) -> None:
        hour = hour_of_day_utc(0)
        table = _table({("TXFD6", hour, "shallow"): 0.3})
        probs = fill_probs_for_flips(np.asarray([0], dtype=np.int64), np.asarray([np.nan]), table, "TXFD6")
        np.testing.assert_allclose(probs, [0.3])


class TestRequiredMoveBounds:
    def test_maker_never_more_optimistic_than_zero_spread_bound(self) -> None:
        hour = hour_of_day_utc(0)
        table = _table({("TXFD6", hour, "shallow"): 0.99, ("TXFD6", hour, "deep"): 0.99})
        res = _result(table, ts=[0, 0], depth=[1.0, 9.0])
        assert res.maker_required_move_threshold_pts >= 2.0 * COST_PER_SIDE

    def test_maker_required_move_never_exceeds_taker(self) -> None:
        taker_required = 2.0 * COST_PER_SIDE + SPREAD
        res = _result(_table(), ts=[0], depth=[1.0])  # fallback p=0.5
        assert res.maker_required_move_threshold_pts <= taker_required
        # And therefore the maker score is never below the taker score.
        taker_score = 3.0 / taker_required
        assert res.maker_cost_survival_score >= taker_score

    def test_zero_fill_prob_degenerates_to_taker_formula(self) -> None:
        hour = hour_of_day_utc(0)
        table = _table({("TXFD6", hour, "shallow"): 0.0})
        res = _result(table, ts=[0], depth=[1.0])
        assert res.maker_required_move_threshold_pts == pytest.approx(2.0 * COST_PER_SIDE + SPREAD)

    def test_no_flip_events_fails_closed_to_taker_formula(self) -> None:
        res = _result(_table(), ts=[], depth=[])
        assert res.n_flip_events == 0
        assert res.maker_fill_prob_mean == 0.0
        assert res.maker_required_move_threshold_pts == pytest.approx(2.0 * COST_PER_SIDE + SPREAD)

    def test_version_stamp(self) -> None:
        res = _result(_table(), ts=[0], depth=[1.0])
        assert res.maker_cost_assumption_version == MAKER_COST_ASSUMPTION_VERSION == "taifex_maker_qhat_v1"
