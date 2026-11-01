"""L1 reads must follow every book update path, not only the one that feeds the Rust state.

Production (2026-10-30): the normalizer attaches ``BookStats`` to every live
BidAsk, so ``process_event`` routes them through ``apply_update_with_stats``,
which never touches ``BookState._rust_state``. Only the boot snapshot went
through ``apply_update``. ``get_l1_scaled`` preferred the Rust state, so it
kept returning the boot snapshot's timestamp and prices; the loss exit's
30 s freshness check then judged every book stale and logged
``loss_exit_unpriced`` once a second for 17 h while the daily-loss HALT held a
lot it could not close.
"""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, BookStats, MetaData, TickEvent
from hft_platform.feed_adapter import lob_engine
from hft_platform.feed_adapter.lob_engine import BookState, LOBEngine

BOOT_TS = 1_000_000_000
LIVE_TS = 61_000_000_000  # one minute later: past the loss exit's 30 s age limit

BOOT_BID, BOOT_ASK = 459_680_000, 459_690_000
LIVE_BID, LIVE_ASK = 450_770_000, 450_780_000


class _FrozenRustState:
    """Stands in for ``RustBookState``: it only changes when ``apply_update`` feeds it."""

    def __init__(self, symbol: str) -> None:
        self.exch_ts = 0
        self.best_bid = 0
        self.best_ask = 0
        self.mid_price_x2 = 0
        self.spread = 0
        self.imbalance = 0.0
        self.bid_depth_total = 0
        self.ask_depth_total = 0

    def apply_update(self, bids, asks, exch_ts) -> None:
        self.exch_ts = int(exch_ts)
        self.best_bid = int(bids[0, 0])
        self.best_ask = int(asks[0, 0])
        self.mid_price_x2 = self.best_bid + self.best_ask
        self.spread = self.best_ask - self.best_bid
        self.bid_depth_total = int(bids[:, 1].sum())
        self.ask_depth_total = int(asks[:, 1].sum())

    def get_l1_scaled(self):
        return (
            self.exch_ts,
            self.best_bid,
            self.best_ask,
            self.mid_price_x2,
            self.spread,
            self.bid_depth_total,
            self.ask_depth_total,
        )

    def get_stats_tuple(self):
        return (
            "STUB",
            self.exch_ts,
            self.mid_price_x2,
            self.spread,
            self.imbalance,
            self.best_bid,
            self.best_ask,
            self.bid_depth_total,
            self.ask_depth_total,
        )


def _book(bid: int, ask: int) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.array([[bid, 3], [bid - 10_000, 2]], dtype=np.int64),
        np.array([[ask, 4], [ask + 10_000, 1]], dtype=np.int64),
    )


def _stats(bid: int, ask: int) -> BookStats:
    return BookStats(bid, ask, 5, 5, (bid + ask) / 2.0, float(ask - bid), 0.0)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> LOBEngine:
    """A LOB engine whose books carry a Rust state, booted from a plain snapshot."""
    monkeypatch.setattr(lob_engine, "_RustBookState", _FrozenRustState)
    monkeypatch.setattr(lob_engine, "_RUST_BOOK_STATE_ENABLED", True)
    eng = LOBEngine()
    bids, asks = _book(BOOT_BID, BOOT_ASK)
    eng.process_event(
        BidAskEvent(meta=MetaData(seq=1, source_ts=BOOT_TS, local_ts=BOOT_TS), symbol="TMFK6", bids=bids, asks=asks)
    )
    assert isinstance(eng.books["TMFK6"]._rust_state, _FrozenRustState)
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
    engine.process_event(
        ("bidask", "TMFK6", bids, asks, LIVE_TS, False, LIVE_BID, LIVE_ASK, 5, 5, 0.0, 0.0, 0.0)
    )

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


def test_get_l1_scaled_matches_rust_fed_update(engine: LOBEngine) -> None:
    """The plain ``apply_update`` path still reports what it was given."""
    bids, asks = _book(LIVE_BID, LIVE_ASK)
    engine.process_event(
        BidAskEvent(meta=MetaData(seq=2, source_ts=LIVE_TS, local_ts=LIVE_TS), symbol="TMFK6", bids=bids, asks=asks)
    )

    assert engine.get_l1_scaled("TMFK6") == (LIVE_TS, LIVE_BID, LIVE_ASK, LIVE_BID + LIVE_ASK, LIVE_ASK - LIVE_BID, 5, 5)


@pytest.mark.skipif(lob_engine._RustBookState is None, reason="rust_core extension not built")
def test_get_l1_scaled_follows_stats_update_with_real_rust_state() -> None:
    book = BookState("TMFK6")
    assert book._rust_state is not None
    bids, asks = _book(BOOT_BID, BOOT_ASK)
    book.apply_update(bids, asks, BOOT_TS)
    bids, asks = _book(LIVE_BID, LIVE_ASK)
    book.apply_update_with_stats(bids, asks, LIVE_TS, _stats(LIVE_BID, LIVE_ASK))

    eng = LOBEngine()
    eng.books["TMFK6"] = book

    ts, bid, ask, *_ = eng.get_l1_scaled("TMFK6")

    assert ts == LIVE_TS
    assert (bid, ask) == (LIVE_BID, LIVE_ASK)
