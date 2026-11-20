"""Tests for crossed-book guard in LOBEngine._recompute() and fused_bypass.

Shioaji can temporarily emit crossed quotes (best_bid > best_ask) during
auction transitions. The guard ensures negative spread never propagates into
FeatureEngine EMA state.

Covers recomputed and precomputed book statistics.
"""

import time

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, FusedBookStats, MetaData
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


# ---------------------------------------------------------------------------
def _make_fused_event(
    bids_price: int,
    asks_price: int,
    mid_price_x2: int,
    spread_scaled: int,
    imbalance: float = 0.5,
    ts: int = 2000,
) -> BidAskEvent:
    bids = np.array([[bids_price, 10]], dtype=np.int64)
    asks = np.array([[asks_price, 10]], dtype=np.int64)
    fs = FusedBookStats(
        best_bid=bids_price,
        best_ask=asks_price,
        bid_depth=10,
        ask_depth=10,
        mid_price_x2=mid_price_x2,
        spread_scaled=spread_scaled,
        imbalance=imbalance,
    )
    return BidAskEvent(
        meta=make_meta(ts),
        symbol="TXFD6",
        bids=bids,
        asks=asks,
        fused_stats=fs,
        is_snapshot=True,
    )


def test_fused_bypass_negative_spread_zeroes_stats(engine: LOBEngine) -> None:
    """Fused bypass: negative spread_scaled must be zeroed by guard."""
    event = _make_fused_event(
        bids_price=5000000,
        asks_price=4990000,
        mid_price_x2=9990000,
        spread_scaled=-10000,  # crossed — the value the guard must reject
        imbalance=0.5,
    )

    stats = engine.process_event(event)

    assert stats.spread_scaled == 0, "Fused bypass must zero spread for negative spread_scaled"
    assert stats.mid_price_x2 == 0, "Fused bypass must zero mid_price_x2 for negative spread_scaled"
    assert stats.imbalance == pytest.approx(0.0), "Fused bypass must zero imbalance for negative spread_scaled"


def test_fused_bypass_normal_book_passes_through(engine: LOBEngine) -> None:
    """Fused bypass: valid (non-crossed) stats pass through unchanged."""
    event = _make_fused_event(
        bids_price=5000000,
        asks_price=5010000,
        mid_price_x2=10010000,
        spread_scaled=10000,
        imbalance=0.3,
    )

    stats = engine.process_event(event)

    assert stats.spread_scaled == 10000
    assert stats.mid_price_x2 == 10010000
    assert stats.imbalance == pytest.approx(0.3)


def test_fused_bypass_depth_totals_unconditional(engine: LOBEngine) -> None:
    """Fused bypass: depth totals are set even when spread is negative (crossed)."""
    event = _make_fused_event(
        bids_price=5000000,
        asks_price=4990000,
        mid_price_x2=9990000,
        spread_scaled=-10000,
        imbalance=0.5,
    )

    engine.process_event(event)

    book = engine.get_book("TXFD6")
    assert book is not None
    assert book.bid_depth_total == 10, "bid_depth_total must be set even for crossed fused book"
    assert book.ask_depth_total == 10, "ask_depth_total must be set even for crossed fused book"
