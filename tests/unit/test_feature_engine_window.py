"""Exact rolling-return and scalar-depth contracts for downstream hot-path work."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import (
    QUALITY_FLAG_GAP,
    QUALITY_FLAG_PARTIAL,
    QUALITY_FLAG_STATE_RESET,
    FeatureEngine,
    _l2_l5_qty,
    _LobKernelState,
    _top_qty,
)


class _NoListBook(np.ndarray):
    def tolist(self):
        raise AssertionError("depth extraction must not materialize a quantity list")


class _CountedReturns(list[int]):
    def __init__(self, values: list[int]) -> None:
        super().__init__(values)
        self.reads = 0

    def __getitem__(self, index):
        self.reads += 1
        return super().__getitem__(index)


@pytest.mark.parametrize("depth", range(6))
@pytest.mark.parametrize("dtype", [np.int64, np.uint64])
def test_depth_quantities_preserve_missing_levels_without_materializing_lists(depth, dtype) -> None:
    # Include a strided input and quantities beyond float's exact integer range.
    levels = np.array([[1_000_000 - k, 2**54 + k] for k in range(10)], dtype=dtype)[::2][:depth]
    levels = levels.view(_NoListBook)
    expected = tuple(float(int(levels[k, 1])) if depth > k else 0.0 for k in range(1, 5))
    result = _l2_l5_qty(levels, depth)
    assert result == expected
    assert all(type(qty) is float for qty in result)


class _NoRowView(np.ndarray):
    def __getitem__(self, key):
        if isinstance(key, int):
            raise AssertionError("top quantity must not allocate a row view")
        return super().__getitem__(key)


@pytest.mark.parametrize("depth", range(6))
def test_top_quantity_uses_scalar_indexing_without_materializing_row_views(depth) -> None:
    levels = np.array([[1_000_000 - k, 10 + k] for k in range(depth)], dtype=np.int64).reshape(depth, 2)
    result = _top_qty(levels.view(_NoRowView))
    assert result == (10 if depth else 0)
    assert type(result) is int


def _stats(mid_x2: int, seq: int) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol="TEST",
        ts=seq * 1_000_000,
        normalizer_seq=seq,
        imbalance=0.0,
        best_bid=mid_x2 // 2,
        best_ask=mid_x2 - mid_x2 // 2,
        bid_depth=20,
        ask_depth=30,
        mid_price_x2=mid_x2,
        spread_scaled=mid_x2 % 2,
    )


def _reference_autocov(returns: list[int]) -> int:
    window = returns[-40:]
    if len(window) < 3:
        return 0
    products = sum(a * b for a, b in zip(window, window[1:]))
    return int(round(float(products) * 1_000_000.0 / float(len(window) - 1)))


def test_return_autocov_matches_window_scan_across_wraps_partial_ooo_and_reset() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2", emit_events=True)
    returns: list[int] = []
    prev_mid = 0
    retained = None
    retained_snapshot = None
    for seq in range(1, 205):
        if seq == 100:
            engine.reset_symbol("TEST")
            returns.clear()
            prev_mid = 0
        if seq in {70, 170}:
            partial = engine.process_lob_stats(_stats(0, seq))
            assert partial is not None
            assert partial.quality_flags & QUALITY_FLAG_PARTIAL
            assert partial.values[17] == _reference_autocov(returns)
            continue
        if seq == 80:
            engine.mark_gap("TEST")
            assert engine.process_lob_stats(_stats(prev_mid + 40, seq - 2)) is None
        # Large alternating deltas exercise exact integer products above int64.
        mid = 2**55 + ((seq * 17) % 23 - 11) * 2**35 + seq % 5
        returns.append(mid - prev_mid if prev_mid else 0)
        prev_mid = mid
        update = engine.process_lob_stats(_stats(mid, seq), local_ts_ns=seq * 1_000_000 + 3)
        assert update is not None
        assert update.values[17] == _reference_autocov(returns)
        assert type(update.values) is tuple
        assert type(update.values[17]) is int
        assert (update.seq, update.ts, update.local_ts) == (seq + (seq >= 80), seq * 1_000_000, seq * 1_000_000 + 3)
        if seq == 80:
            assert update.quality_flags & QUALITY_FLAG_GAP
        if seq == 100:
            assert update.quality_flags & QUALITY_FLAG_STATE_RESET
        if retained is None:
            retained = update
            retained_snapshot = (update.seq, update.ts, update.local_ts, update.values)
        else:
            assert update is not retained
            assert (retained.seq, retained.ts, retained.local_ts, retained.values) == retained_snapshot


def test_return_autocov_reads_bounded_entries_when_window_is_full() -> None:
    engine = FeatureEngine(feature_set_id="lob_shared_v2")
    kernel = _LobKernelState()
    for seq in range(1, 45):
        engine._compute_v2_features(kernel, _stats(2_000_000 + seq, seq), 2_000_000 + seq, 20, 30, 1_000_000, 1_000_001)
    counted = _CountedReturns(kernel.ret_buf)
    kernel.ret_buf = counted
    returns = [0] + [1] * 43
    for seq in range(45, 125):
        reads_before = counted.reads
        mid = 2_000_000 + seq * 3
        returns.append(mid - kernel.prev_mid_price_x2)
        result = engine._compute_v2_features(kernel, _stats(mid, seq), mid, 20, 30, 1_000_000, 1_000_001)
        assert result[1] == _reference_autocov(returns)
        # A full-window update needs only the outgoing pair and previous newest.
        assert counted.reads - reads_before <= 3
