"""Tests for crossed-book guard in LOBEngine._recompute().

Shioaji can temporarily emit crossed quotes (best_bid > best_ask) during
auction transitions. The guard ensures negative spread never propagates into
FeatureEngine EMA state.
"""

import time

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, MetaData
from hft_platform.feed_adapter.lob_engine import LOBEngine


def make_meta(ts: int = 0) -> MetaData:
    return MetaData(seq=1, topic="test", source_ts=ts, local_ts=time.time_ns())


def make_event(bids_price: int, asks_price: int, ts: int = 1000) -> BidAskEvent:
    bids = np.array([[bids_price, 10]], dtype=np.int64)
    asks = np.array([[asks_price, 10]], dtype=np.int64)
    return BidAskEvent(
        meta=make_meta(ts),
        symbol="TXFD6",
        bids=bids,
        asks=asks,
        is_snapshot=True,
    )


@pytest.fixture
def engine() -> LOBEngine:
    return LOBEngine()


def test_crossed_book_sets_zero_stats(engine: LOBEngine) -> None:
    """Crossed book (best_bid > best_ask) must zero out stats."""
    # bid=5000000, ask=4990000 → crossed
    stats = engine.process_event(make_event(bids_price=5000000, asks_price=4990000))

    assert stats.spread_scaled == 0, "spread must be 0 for a crossed book"
    assert stats.mid_price_x2 == 0, "mid_price_x2 must be 0 for a crossed book"
    assert stats.imbalance == pytest.approx(0.0), "imbalance must be 0.0 for a crossed book"


def test_normal_book_sets_positive_spread(engine: LOBEngine) -> None:
    """Normal book (bid < ask) must produce a positive spread."""
    stats = engine.process_event(make_event(bids_price=5000000, asks_price=5010000))

    assert stats.spread_scaled > 0, "spread must be positive for a normal book"
    assert stats.mid_price_x2 == 5000000 + 5010000
    assert stats.spread_scaled == 10000


def test_crossed_book_after_normal_resets_stats(engine: LOBEngine) -> None:
    """Stats must reset to zero when a normal book is followed by a crossed book."""
    # First: establish a valid book with positive spread
    stats_normal = engine.process_event(make_event(bids_price=5000000, asks_price=5010000, ts=1000))
    assert stats_normal.spread_scaled > 0  # sanity check

    # Then: crossed book arrives
    stats_crossed = engine.process_event(make_event(bids_price=5000000, asks_price=4990000, ts=1001))
    assert stats_crossed.spread_scaled == 0, "spread must reset to 0 after crossed book"
    assert stats_crossed.mid_price_x2 == 0, "mid_price_x2 must reset to 0 after crossed book"
    assert stats_crossed.imbalance == pytest.approx(0.0), "imbalance must reset to 0.0 after crossed book"


def test_touched_book_equal_bid_ask_is_valid(engine: LOBEngine) -> None:
    """Touched book (bid == ask) is allowed — spread is zero but stats are valid."""
    stats = engine.process_event(make_event(bids_price=5000000, asks_price=5000000))

    # best_ask >= best_bid is satisfied (equal), so mid_price_x2 is set
    assert stats.mid_price_x2 == 10000000
    assert stats.spread_scaled == 0
