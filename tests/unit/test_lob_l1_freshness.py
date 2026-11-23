"""Verify L1 reads and timestamp freshness across all book update paths."""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, BookStats, MetaData, TickEvent
from hft_platform.feed_adapter.lob_engine import LOBEngine

BOOT_TS = 1_000_000_000
LIVE_TS = 61_000_000_000  # one minute later: past the loss exit's 30 s age limit

BOOT_BID, BOOT_ASK = 459_680_000, 459_690_000
LIVE_BID, LIVE_ASK = 450_770_000, 450_780_000


def _book(bid: int, ask: int) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.array([[bid, 3], [bid - 10_000, 2]], dtype=np.int64),
        np.array([[ask, 4], [ask + 10_000, 1]], dtype=np.int64),
    )


def _stats(bid: int, ask: int) -> BookStats:
    return BookStats(bid, ask, 5, 5, (bid + ask) / 2.0, float(ask - bid), 0.0)


@pytest.fixture
def engine() -> LOBEngine:
    eng = LOBEngine()
    bids, asks = _book(BOOT_BID, BOOT_ASK)
    eng.process_event(
        BidAskEvent(meta=MetaData(seq=1, source_ts=BOOT_TS, local_ts=BOOT_TS), symbol="TMFK6", bids=bids, asks=asks)
    )
    return eng


def test_get_l1_scaled_follows_stats_update_after_snapshot(engine: LOBEngine) -> None:
    bids, asks = _book(LIVE_BID, LIVE_ASK)
    engine.process_event(
        BidAskEvent(
            meta=MetaData(seq=2, source_ts=LIVE_TS, local_ts=LIVE_TS),
            symbol="TMFK6",
            bids=bids,
            asks=asks,
            stats=_stats(LIVE_BID, LIVE_ASK),
        )
    )

    ts, bid, ask, mid_x2, spread, _bd, _ad = engine.get_l1_scaled("TMFK6")

    assert ts == LIVE_TS
    assert (bid, ask) == (LIVE_BID, LIVE_ASK)
    assert mid_x2 == LIVE_BID + LIVE_ASK
    assert spread == LIVE_ASK - LIVE_BID


def test_get_l1_scaled_follows_tuple_stats_update_after_snapshot(engine: LOBEngine) -> None:
    bids, asks = _book(LIVE_BID, LIVE_ASK)
    engine.process_event(("bidask", "TMFK6", bids, asks, LIVE_TS, False, LIVE_BID, LIVE_ASK, 5, 5, 0.0, 0.0, 0.0))

    ts, bid, ask, *_ = engine.get_l1_scaled("TMFK6")

    assert ts == LIVE_TS
    assert (bid, ask) == (LIVE_BID, LIVE_ASK)


def test_get_l1_scaled_timestamp_advances_on_tick_after_snapshot(engine: LOBEngine) -> None:
    engine.process_event(
        TickEvent(
            meta=MetaData(seq=2, source_ts=LIVE_TS, local_ts=LIVE_TS),
            symbol="TMFK6",
            price=LIVE_BID,
            volume=1,
            total_volume=1,
            bid_side_total_vol=0,
            ask_side_total_vol=0,
            is_simtrade=False,
            is_odd_lot=False,
        )
    )

    assert engine.get_l1_scaled("TMFK6")[0] == LIVE_TS


def test_get_stats_tuple_follows_stats_update_after_snapshot(engine: LOBEngine) -> None:
    bids, asks = _book(LIVE_BID, LIVE_ASK)
    book = engine.books["TMFK6"]
    book.apply_update_with_stats(bids, asks, LIVE_TS, _stats(LIVE_BID, LIVE_ASK))

    tag, symbol, ts, mid_x2, spread, _imb, bid, ask, _bd, _ad = book.get_stats_tuple()

    assert (tag, symbol) == ("lobstats", "TMFK6")
    assert ts == LIVE_TS
    assert (bid, ask) == (LIVE_BID, LIVE_ASK)
    assert mid_x2 == LIVE_BID + LIVE_ASK
    assert spread == LIVE_ASK - LIVE_BID


def test_get_l1_scaled_matches_book_update(engine: LOBEngine) -> None:
    """The plain ``apply_update`` path still reports what it was given."""
    bids, asks = _book(LIVE_BID, LIVE_ASK)
    engine.process_event(
        BidAskEvent(meta=MetaData(seq=2, source_ts=LIVE_TS, local_ts=LIVE_TS), symbol="TMFK6", bids=bids, asks=asks)
    )

    assert engine.get_l1_scaled("TMFK6") == (
        LIVE_TS,
        LIVE_BID,
        LIVE_ASK,
        LIVE_BID + LIVE_ASK,
        LIVE_ASK - LIVE_BID,
        5,
        5,
    )
