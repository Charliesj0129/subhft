"""Tests for crossed-book guard in LOBEngine._recompute() and fused_bypass.

Shioaji can temporarily emit crossed quotes (best_bid > best_ask) during
auction transitions. The guard ensures negative spread never propagates into
FeatureEngine EMA state.

Covers:
- Python fallback path (tested implicitly via process_event when Rust unavailable)
- Rust compute_book_stats fast path (same process_event path with numpy arrays)
- Fused bypass path (direct fused_stats injection via monkeypatch)
"""

import time
from unittest.mock import patch

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
# Rust compute_book_stats path inside _recompute():
# _recompute() is called when RustBookState (rs) is None.
# Force rs=None so _recompute() runs, then mock _RUST_COMPUTE_STATS.
# ---------------------------------------------------------------------------


def _force_recompute_path(engine: LOBEngine, event: BidAskEvent, mock_return: tuple) -> None:
    """Process event with RustBookState disabled so _recompute() uses _RUST_COMPUTE_STATS."""
    engine.process_event(event)  # ensure book is created
    book = engine.get_book(event.symbol)
    assert book is not None
    # Force _rust_state=None so apply_update falls back to _recompute()
    object.__setattr__(book, "_rust_state", None) if hasattr(book, "__slots__") else setattr(book, "_rust_state", None)


def test_rust_compute_stats_crossed_book_zeroes_stats(engine: LOBEngine) -> None:
    """_recompute() Rust path: when compute_book_stats returns crossed prices, guard zeroes stats."""
    crossed_rust_return = (
        5000000,  # best_bid
        4990000,  # best_ask (< best_bid → crossed)
        100,  # bid_depth_total
        80,  # ask_depth_total
        0,  # mid_price (ignored after guard)
        -10000,  # spread (negative — the bug this guards against)
        0.5,  # imbalance
    )
    event = make_event(bids_price=5000000, asks_price=4990000)
    # Ensure book exists then null out RustBookState to reach _recompute()
    engine.process_event(event)
    book = engine.get_book("TXFD6")
    assert book is not None
    book._rust_state = None

    with patch("hft_platform.feed_adapter.lob_engine._RUST_COMPUTE_STATS", return_value=crossed_rust_return):
        with patch("hft_platform.feed_adapter.lob_engine._RUST_ENABLED", True):
            stats = engine.process_event(make_event(bids_price=5000000, asks_price=4990000, ts=1001))

    assert stats.spread_scaled == 0, "_recompute() Rust path must zero spread for crossed book"
    assert stats.mid_price_x2 == 0, "_recompute() Rust path must zero mid_price_x2 for crossed book"
    assert stats.imbalance == pytest.approx(0.0), "_recompute() Rust path must zero imbalance for crossed book"


def test_rust_compute_stats_depth_totals_unconditional(engine: LOBEngine) -> None:
    """_recompute() Rust path: depth totals are set unconditionally even for a crossed book."""
    crossed_rust_return = (
        5000000,  # best_bid
        4990000,  # best_ask (crossed)
        120,  # bid_depth_total
        90,  # ask_depth_total
        0,
        -10000,
        0.4,
    )
    engine.process_event(make_event(bids_price=5000000, asks_price=4990000))
    book = engine.get_book("TXFD6")
    assert book is not None
    book._rust_state = None

    with patch("hft_platform.feed_adapter.lob_engine._RUST_COMPUTE_STATS", return_value=crossed_rust_return):
        with patch("hft_platform.feed_adapter.lob_engine._RUST_ENABLED", True):
            engine.process_event(make_event(bids_price=5000000, asks_price=4990000, ts=1001))

    assert book.bid_depth_total == 120, "bid_depth_total must be set even for crossed book"
    assert book.ask_depth_total == 90, "ask_depth_total must be set even for crossed book"


# ---------------------------------------------------------------------------
# Fused bypass path: inject FusedBookStats with negative spread_scaled
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
    """Fused bypass: negative spread_scaled from Rust must be zeroed by guard."""
    event = _make_fused_event(
        bids_price=5000000,
        asks_price=4990000,
        mid_price_x2=9990000,
        spread_scaled=-10000,  # crossed — the value the guard must reject
        imbalance=0.5,
    )

    with patch("hft_platform.feed_adapter.lob_engine._FUSED_BYPASS", True):
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

    with patch("hft_platform.feed_adapter.lob_engine._FUSED_BYPASS", True):
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

    with patch("hft_platform.feed_adapter.lob_engine._FUSED_BYPASS", True):
        engine.process_event(event)

    book = engine.get_book("TXFD6")
    assert book is not None
    assert book.bid_depth_total == 10, "bid_depth_total must be set even for crossed fused book"
    assert book.ask_depth_total == 10, "ask_depth_total must be set even for crossed fused book"
