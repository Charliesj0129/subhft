"""prim_v1 primitive/transform semantics on synthetic panels (spec §7)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from research.candidate_loop.primitives import (
    ZSCORE_MIN_VALID,
    book_imbalance,
    clip,
    depth_delta,
    depth_sum,
    ema,
    future_mid_return,
    parse_canonical_window,
    rolling_zscore,
    safe_divide,
    trade_imbalance,
    trailing_anchor_indices,
)
from research.candidate_loop.schema import Window


def _cols(
    *,
    local_ts: list[int],
    bid_qty: dict[int, list[float]] | None = None,
    ask_qty: dict[int, list[float]] | None = None,
    mid: list[float] | None = None,
    trade_buy: list[float] | None = None,
    trade_sell: list[float] | None = None,
) -> dict[str, np.ndarray]:
    n = len(local_ts)
    cols: dict[str, np.ndarray] = {
        "exch_ts": np.asarray(local_ts, dtype=np.int64),
        "local_ts": np.asarray(local_ts, dtype=np.int64),
        "mid": np.asarray(mid if mid is not None else [100.0] * n),
        "trade_buy_qty": np.asarray(trade_buy if trade_buy is not None else [0.0] * n),
        "trade_sell_qty": np.asarray(trade_sell if trade_sell is not None else [0.0] * n),
    }
    for lvl in range(1, 6):
        cols[f"bid_qty_{lvl}"] = np.asarray((bid_qty or {}).get(lvl, [0.0] * n))
        cols[f"ask_qty_{lvl}"] = np.asarray((ask_qty or {}).get(lvl, [0.0] * n))
    return cols


class TestParseCanonicalWindow:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("2000_events", Window(kind="events", count=2000)),
            ("1000000000ns", Window(kind="time", duration_ns=1_000_000_000)),
            ("500ms", Window(kind="time", duration_ns=500_000_000)),
            ("5s", Window(kind="time", duration_ns=5_000_000_000)),
        ],
    )
    def test_accepts_canonical_and_raw_forms(self, spec: str, expected: Window) -> None:
        assert parse_canonical_window(spec) == expected

    @pytest.mark.parametrize("spec", ["0ns", "garbage", "ns"])
    def test_rejects_malformed(self, spec: str) -> None:
        with pytest.raises(ValueError):
            parse_canonical_window(spec)


class TestSafeDivide:
    def test_zero_denominator_yields_zero(self) -> None:
        out = safe_divide(np.array([1.0, 2.0]), np.array([0.0, 4.0]))
        np.testing.assert_allclose(out, [0.0, 0.5])

    def test_nan_passes_through(self) -> None:
        out = safe_divide(np.array([np.nan, 1.0]), np.array([2.0, np.nan]))
        assert math.isnan(out[0]) and math.isnan(out[1])


class TestDepthAndImbalance:
    def test_depth_sum_adds_requested_levels_only(self) -> None:
        cols = _cols(local_ts=[0, 1], bid_qty={1: [1.0, 2.0], 2: [10.0, 20.0], 3: [100.0, 200.0]})
        np.testing.assert_allclose(depth_sum(cols, "bid", 2), [11.0, 22.0])

    def test_book_imbalance_known_values_and_empty_book_zero(self) -> None:
        cols = _cols(local_ts=[0, 1, 2], bid_qty={1: [3.0, 1.0, 0.0]}, ask_qty={1: [1.0, 3.0, 0.0]})
        np.testing.assert_allclose(book_imbalance(cols, 1), [0.5, -0.5, 0.0])

    def test_depth_delta_event_window_with_nan_warmup(self) -> None:
        cols = _cols(local_ts=list(range(5)), bid_qty={1: [1.0, 3.0, 6.0, 10.0, 15.0]})
        out = depth_delta(cols, "bid", 1, Window(kind="events", count=2))
        assert np.isnan(out[0]) and np.isnan(out[1])
        np.testing.assert_allclose(out[2:], [5.0, 7.0, 9.0])

    def test_depth_delta_time_window_anchors_at_or_before_cutoff(self) -> None:
        # Rows at 0,1,2,5 s; window 2s at t=5 anchors to the row at t=2.
        sec = 1_000_000_000
        cols = _cols(local_ts=[0, sec, 2 * sec, 5 * sec], bid_qty={1: [1.0, 2.0, 4.0, 7.0]})
        out = depth_delta(cols, "bid", 1, Window(kind="time", duration_ns=2 * sec))
        assert np.isnan(out[0]) and np.isnan(out[1])
        np.testing.assert_allclose(out[2:], [3.0, 3.0])


class TestTrailingAnchors:
    def test_time_anchor_is_last_row_at_or_before_cutoff(self) -> None:
        ts = np.array([0, 10, 20, 30], dtype=np.int64)
        anchors = trailing_anchor_indices(ts, Window(kind="time", duration_ns=10))
        np.testing.assert_array_equal(anchors, [-1, 0, 1, 2])

    def test_event_anchor_is_index_minus_n(self) -> None:
        ts = np.arange(4, dtype=np.int64)
        anchors = trailing_anchor_indices(ts, Window(kind="events", count=3))
        np.testing.assert_array_equal(anchors, [-3, -2, -1, 0])


class TestTradeImbalance:
    def test_window_deltas_from_cumulative_arrays(self) -> None:
        cols = _cols(
            local_ts=list(range(4)),
            trade_buy=[0.0, 3.0, 3.0, 6.0],
            trade_sell=[0.0, 1.0, 1.0, 2.0],
        )
        out = trade_imbalance(cols, Window(kind="events", count=1))
        assert np.isnan(out[0])
        # deltas: (3-1)/(3+1), no trades -> 0, (3-1)/(3+1)
        np.testing.assert_allclose(out[1:], [0.5, 0.0, 0.5])

    def test_no_trades_in_window_is_zero_not_nan(self) -> None:
        cols = _cols(local_ts=list(range(3)), trade_buy=[5.0, 5.0, 5.0], trade_sell=[2.0, 2.0, 2.0])
        out = trade_imbalance(cols, Window(kind="events", count=1))
        np.testing.assert_allclose(out[1:], [0.0, 0.0])


class TestFutureMidReturn:
    def test_time_horizon_uses_asof_join_and_nan_past_end(self) -> None:
        sec = 1_000_000_000
        cols = _cols(local_ts=[0, sec, 2 * sec, 3 * sec], mid=[100.0, 101.0, 102.0, 104.0])
        out = future_mid_return(cols, Window(kind="time", duration_ns=sec))
        np.testing.assert_allclose(out[:3], [0.01, 102.0 / 101.0 - 1.0, 104.0 / 102.0 - 1.0])
        assert np.isnan(out[3])

    def test_time_horizon_between_rows_picks_last_at_or_before(self) -> None:
        sec = 1_000_000_000
        cols = _cols(local_ts=[0, sec, 3 * sec], mid=[100.0, 110.0, 120.0])
        out = future_mid_return(cols, Window(kind="time", duration_ns=2 * sec))
        # t=0 + 2s lands between rows 1 (1s) and 2 (3s) -> asof row 1.
        assert out[0] == pytest.approx(0.1)
        # t=1s + 2s == 3s -> row 2 exactly.
        assert out[1] == pytest.approx(120.0 / 110.0 - 1.0)
        assert np.isnan(out[2])

    def test_event_horizon_shifts_by_n_rows(self) -> None:
        cols = _cols(local_ts=list(range(4)), mid=[100.0, 101.0, 102.0, 103.0])
        out = future_mid_return(cols, Window(kind="events", count=2))
        np.testing.assert_allclose(out[:2], [0.02, 103.0 / 101.0 - 1.0])
        assert np.isnan(out[2]) and np.isnan(out[3])

    def test_empty_panel_returns_empty(self) -> None:
        cols = _cols(local_ts=[])
        assert future_mid_return(cols, Window(kind="events", count=1)).size == 0


class TestRollingZscore:
    def test_event_window_population_moments(self) -> None:
        ts = np.arange(4, dtype=np.int64)
        x = np.array([1.0, 2.0, 3.0, 4.0])
        out = rolling_zscore(x, ts, Window(kind="events", count=3))
        assert np.isnan(out[0]) and np.isnan(out[1])
        std = math.sqrt(2.0 / 3.0)  # population std of 3 consecutive ints
        np.testing.assert_allclose(out[2:], [1.0 / std, 1.0 / std])

    def test_nan_inputs_excluded_from_moments(self) -> None:
        ts = np.arange(4, dtype=np.int64)
        x = np.array([1.0, np.nan, 3.0, 4.0])
        out = rolling_zscore(x, ts, Window(kind="events", count=3))
        assert np.isnan(out[1])  # current value NaN
        # window [nan,3,4]: mean 3.5, population std 0.5 -> (4-3.5)/0.5
        assert out[3] == pytest.approx(1.0)

    def test_constant_window_is_nan_not_inf(self) -> None:
        ts = np.arange(3, dtype=np.int64)
        out = rolling_zscore(np.array([5.0, 5.0, 5.0]), ts, Window(kind="events", count=3))
        assert np.isnan(out[2])

    def test_time_window_warmup_is_nan(self) -> None:
        ts = np.array([0, 10, 20, 30], dtype=np.int64)
        x = np.array([1.0, 2.0, 3.0, 4.0])
        out = rolling_zscore(x, ts, Window(kind="time", duration_ns=20))
        # Rows 0/1: window reaches before the first row -> warmup NaN.
        assert np.isnan(out[0]) and np.isnan(out[1])
        # Window is (t-d, t]: at t=20 it holds rows [2,3] -> mean 2.5, std 0.5.
        assert out[2] == pytest.approx(1.0)
        assert out[3] == pytest.approx(1.0)

    def test_min_valid_count_guard(self) -> None:
        assert ZSCORE_MIN_VALID >= 2
        ts = np.arange(3, dtype=np.int64)
        x = np.array([np.nan, np.nan, 1.0])
        out = rolling_zscore(x, ts, Window(kind="events", count=3))
        assert np.isnan(out[2])  # only 1 finite value in the window


class TestEma:
    def test_event_alpha_is_two_over_n_plus_one(self) -> None:
        ts = np.arange(3, dtype=np.int64)
        out = ema(np.array([0.0, 0.0, 1.0]), ts, Window(kind="events", count=3))
        alpha = 2.0 / 4.0
        np.testing.assert_allclose(out, [0.0, 0.0, alpha])

    def test_constant_input_stays_constant(self) -> None:
        ts = np.arange(3, dtype=np.int64)
        out = ema(np.array([7.0, 7.0, 7.0]), ts, Window(kind="events", count=10))
        np.testing.assert_allclose(out, [7.0, 7.0, 7.0])

    def test_nan_carries_previous_value(self) -> None:
        ts = np.arange(3, dtype=np.int64)
        out = ema(np.array([np.nan, 4.0, np.nan]), ts, Window(kind="events", count=3))
        assert np.isnan(out[0])
        assert out[1] == 4.0 and out[2] == 4.0

    def test_time_window_exponential_decay(self) -> None:
        ts = np.array([0, 100], dtype=np.int64)
        out = ema(np.array([0.0, 1.0]), ts, Window(kind="time", duration_ns=100))
        w = math.exp(-1.0)
        assert out[1] == pytest.approx(w * 0.0 + (1.0 - w) * 1.0)


class TestClip:
    def test_clip_bounds_and_nan_passthrough(self) -> None:
        out = clip(np.array([-5.0, 0.0, 5.0, np.nan]), -1.0, 1.0)
        np.testing.assert_allclose(out[:3], [-1.0, 0.0, 1.0])
        assert np.isnan(out[3])
