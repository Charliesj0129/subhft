"""Edge case tests for LOBEngine.

Covers single-sided books, multi-symbol isolation, stale update rejection,
empty books, and direct stat computation verification.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, MetaData
from hft_platform.feed_adapter.lob_engine import LOBEngine


def _make_meta(ts: int = 0) -> MetaData:
    return MetaData(seq=1, topic="test", source_ts=ts, local_ts=time.time_ns())


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("HFT_RUST_LOB", "0")
    return LOBEngine()


# ---------- 1. Single-sided book: only bids ----------


def test_single_sided_book_only_bids(engine):
    """All bids, no asks -> mid_price_x2 and spread should be 0 (no valid two-sided quote)."""
    bids = np.array([[4_990_000, 10], [4_980_000, 20]], dtype=np.int64)
    asks = np.empty((0, 2), dtype=np.int64)

    event = BidAskEvent(
        meta=_make_meta(1000),
        symbol="2330",
        bids=bids,
        asks=asks,
        is_snapshot=True,
    )
    stats = engine.process_event(event)

    assert stats is not None
    book = engine.get_book("2330")
    # With only bids, best_ask=0 so the code falls into the else branch:
    # mid_price_x2=0, spread=0, imbalance=0.0
    assert book.mid_price_x2 == 0
    assert book.spread == 0
    assert book.imbalance == 0.0
    # Bid depth should still be computed
    assert book.bid_depth_total == 30  # 10 + 20
    assert book.ask_depth_total == 0


# ---------- 2. Single-sided book: only asks ----------


def test_single_sided_book_only_asks(engine):
    """All asks, no bids -> mid_price_x2 and spread should be 0."""
    bids = np.empty((0, 2), dtype=np.int64)
    asks = np.array([[5_010_000, 15], [5_020_000, 25]], dtype=np.int64)

    event = BidAskEvent(
        meta=_make_meta(1000),
        symbol="2330",
        bids=bids,
        asks=asks,
        is_snapshot=True,
    )
    stats = engine.process_event(event)

    assert stats is not None
    book = engine.get_book("2330")
    assert book.mid_price_x2 == 0
    assert book.spread == 0
    assert book.imbalance == 0.0
    assert book.bid_depth_total == 0
    assert book.ask_depth_total == 40  # 15 + 25


# ---------- 3. Multi-symbol coexistence ----------


def test_multi_symbol_coexistence(engine):
    """Two symbols in the same engine must be fully isolated."""
    bids_a = np.array([[1_000_000, 5]], dtype=np.int64)
    asks_a = np.array([[1_010_000, 10]], dtype=np.int64)

    bids_b = np.array([[2_000_000, 20]], dtype=np.int64)
    asks_b = np.array([[2_020_000, 30]], dtype=np.int64)

    engine.process_event(BidAskEvent(meta=_make_meta(100), symbol="AAAA", bids=bids_a, asks=asks_a))
    engine.process_event(BidAskEvent(meta=_make_meta(200), symbol="BBBB", bids=bids_b, asks=asks_b))

    book_a = engine.get_book("AAAA")
    book_b = engine.get_book("BBBB")

    # Symbol A
    assert book_a.mid_price_x2 == 1_000_000 + 1_010_000  # 2_010_000
    assert book_a.spread == 1_010_000 - 1_000_000  # 10_000
    assert book_a.bid_depth_total == 5
    assert book_a.ask_depth_total == 10

    # Symbol B
    assert book_b.mid_price_x2 == 2_000_000 + 2_020_000  # 4_020_000
    assert book_b.spread == 2_020_000 - 2_000_000  # 20_000
    assert book_b.bid_depth_total == 20
    assert book_b.ask_depth_total == 30

    # Updating one symbol must not affect the other
    engine.process_event(
        BidAskEvent(
            meta=_make_meta(300),
            symbol="AAAA",
            bids=np.array([[1_050_000, 99]], dtype=np.int64),
            asks=np.array([[1_060_000, 99]], dtype=np.int64),
        )
    )
    # A changed
    assert book_a.mid_price_x2 == 1_050_000 + 1_060_000
    # B unchanged
    assert book_b.mid_price_x2 == 4_020_000


# ---------- 4. get_l1_scaled basic ----------


def test_get_l1_scaled_basic(engine):
    """get_l1_scaled returns correct L1 tuple for a known book."""
    bids = np.array([[4_990_000, 10]], dtype=np.int64)
    asks = np.array([[5_010_000, 20]], dtype=np.int64)
    engine.process_event(BidAskEvent(meta=_make_meta(5000), symbol="2330", bids=bids, asks=asks))

    l1 = engine.get_l1_scaled("2330")
    assert l1 is not None
    ts, best_bid, best_ask, mid_x2, spread, bid_depth, ask_depth = l1

    assert ts == 5000
    assert best_bid == 4_990_000
    assert best_ask == 5_010_000
    assert mid_x2 == 4_990_000 + 5_010_000  # 10_000_000
    assert spread == 5_010_000 - 4_990_000  # 20_000
    assert bid_depth == 10
    assert ask_depth == 20


# ---------- 5. get_l1_scaled unknown symbol ----------


def test_get_l1_scaled_unknown_symbol(engine):
    """get_l1_scaled returns None for a symbol never seen."""
    result = engine.get_l1_scaled("NOSUCH")
    assert result is None


# ---------- 6. Stale update rejected ----------


def test_stale_update_rejected(engine):
    """Event with exch_ts < current exch_ts should be ignored."""
    bids_new = np.array([[5_000_000, 10]], dtype=np.int64)
    asks_new = np.array([[5_010_000, 20]], dtype=np.int64)

    # First update at ts=2000
    engine.process_event(BidAskEvent(meta=_make_meta(2000), symbol="2330", bids=bids_new, asks=asks_new))
    book = engine.get_book("2330")
    assert book.exch_ts == 2000
    original_mid = book.mid_price_x2

    # Stale update at ts=1000 with completely different prices
    bids_old = np.array([[3_000_000, 5]], dtype=np.int64)
    asks_old = np.array([[3_010_000, 5]], dtype=np.int64)
    engine.process_event(BidAskEvent(meta=_make_meta(1000), symbol="2330", bids=bids_old, asks=asks_old))

    # Book should be unchanged
    assert book.exch_ts == 2000
    assert book.mid_price_x2 == original_mid
    assert book.bid_depth_total == 10
    assert book.ask_depth_total == 20


# ---------- 7. Empty book handling ----------


def test_empty_book_handling(engine):
    """Empty bids and asks arrays should produce zero stats without crashing."""
    event = BidAskEvent(
        meta=_make_meta(1000),
        symbol="2330",
        bids=np.empty((0, 2), dtype=np.int64),
        asks=np.empty((0, 2), dtype=np.int64),
    )
    stats = engine.process_event(event)
    assert stats is not None

    book = engine.get_book("2330")
    assert book.mid_price_x2 == 0
    assert book.spread == 0
    assert book.imbalance == 0.0
    assert book.bid_depth_total == 0
    assert book.ask_depth_total == 0


# ---------- 8. Spread calculation ----------


def test_spread_calculation(engine):
    """Known bid/ask -> spread_scaled = ask - bid."""
    bid_price = 4_990_000
    ask_price = 5_010_000
    expected_spread = ask_price - bid_price  # 20_000

    bids = np.array([[bid_price, 10]], dtype=np.int64)
    asks = np.array([[ask_price, 10]], dtype=np.int64)
    event = BidAskEvent(meta=_make_meta(1000), symbol="2330", bids=bids, asks=asks)
    stats = engine.process_event(event)

    assert stats.spread_scaled == expected_spread
    book = engine.get_book("2330")
    assert book.spread == expected_spread


# ---------- 9. Imbalance calculation ----------


def test_imbalance_calculation(engine):
    """Known depths -> imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)."""
    bid_vol = 30
    ask_vol = 10
    expected_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)  # 0.5

    bids = np.array([[5_000_000, bid_vol]], dtype=np.int64)
    asks = np.array([[5_010_000, ask_vol]], dtype=np.int64)
    event = BidAskEvent(meta=_make_meta(1000), symbol="2330", bids=bids, asks=asks)
    stats = engine.process_event(event)

    assert stats.imbalance == pytest.approx(expected_imbalance)

    # Symmetric case: equal volumes -> imbalance = 0
    bids_eq = np.array([[5_000_000, 50]], dtype=np.int64)
    asks_eq = np.array([[5_010_000, 50]], dtype=np.int64)
    event_eq = BidAskEvent(meta=_make_meta(1001), symbol="SYM", bids=bids_eq, asks=asks_eq)
    stats_eq = engine.process_event(event_eq)
    assert stats_eq.imbalance == pytest.approx(0.0)


# ---------- 10. mid_price_x2 calculation ----------


def test_mid_price_x2_calculation(engine):
    """bid=4990000, ask=5010000 -> mid_price_x2 = 10000000."""
    bids = np.array([[4_990_000, 10]], dtype=np.int64)
    asks = np.array([[5_010_000, 10]], dtype=np.int64)
    event = BidAskEvent(meta=_make_meta(1000), symbol="2330", bids=bids, asks=asks)
    stats = engine.process_event(event)

    assert stats.mid_price_x2 == 10_000_000
    book = engine.get_book("2330")
    assert book.mid_price_x2 == 10_000_000
