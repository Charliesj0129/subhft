"""Small-book scalar reductions retain numpy's exact depth semantics."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.feed_adapter.lob_engine import BookState


class _NoColumnView(np.ndarray):
    def __getitem__(self, key):
        if isinstance(key, tuple) and any(isinstance(part, slice) for part in key):
            raise AssertionError("top-five depth must not allocate a column view")
        return super().__getitem__(key)


@pytest.mark.parametrize("bid_depth", range(6))
@pytest.mark.parametrize("ask_depth", range(6))
def test_small_book_depth_uses_scalars_and_preserves_empty_sides(bid_depth, ask_depth) -> None:
    bids = np.array([[1_000_000 - k, 10 + k] for k in range(bid_depth)], dtype=np.int64).reshape(bid_depth, 2)
    asks = np.array([[1_010_000 + k, 20 + k] for k in range(ask_depth)], dtype=np.int64).reshape(ask_depth, 2)
    book = BookState("TEST")
    book.apply_update(bids.view(_NoColumnView), asks.view(_NoColumnView), 1_000_000)
    stats = book.get_stats()
    assert stats.bid_depth == sum(10 + k for k in range(bid_depth))
    assert stats.ask_depth == sum(20 + k for k in range(ask_depth))
    assert stats.best_bid == (1_000_000 if bid_depth else 0)
    assert stats.best_ask == (1_010_000 if ask_depth else 0)
    assert stats.mid_price_x2 == (2_010_000 if bid_depth and ask_depth else 0)
    assert stats.spread_scaled == (10_000 if bid_depth and ask_depth else 0)
    assert type(stats.bid_depth) is int
    assert type(stats.ask_depth) is int


@pytest.mark.parametrize(
    "quantities",
    [
        [2**63 - 1, 1],
        [-(2**63), -1],
        [2**63 - 1] * 5,
        [-(2**63)] * 5,
        [2**63 - 1, -(2**63), 2**63 - 1, -(2**63), -100],
    ],
)
def test_small_book_depth_preserves_numpy_signed_overflow(quantities) -> None:
    levels = np.array([[1_000_000, qty] for qty in quantities], dtype=np.int64)
    expected = int(levels[:, 1].sum())
    book = BookState("TEST")
    book.apply_update(levels, levels, 1_000_000)
    assert book.bid_depth_total == expected
    assert book.ask_depth_total == expected


@pytest.mark.parametrize("dtype", [np.int32, np.uint64, np.float64])
@pytest.mark.parametrize("depth", [1, 5, 6])
def test_book_depth_preserves_other_dtype_and_wider_book_reductions(dtype, depth, monkeypatch) -> None:
    from hft_platform.feed_adapter import lob_engine

    monkeypatch.setattr(lob_engine, "_FORCE_NUMPY", False)
    levels = np.array([[1_000_000 - k, 2**31 - 1] for k in range(depth)], dtype=dtype)
    expected = int(levels[:, 1].sum())
    book = BookState("TEST")
    book.apply_update(levels, levels, 1_000_000)
    assert book.bid_depth_total == expected
    assert book.ask_depth_total == expected
