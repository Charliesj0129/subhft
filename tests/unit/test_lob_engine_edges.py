"""Edge case tests for LOBEngine.

Covers single-sided books, multi-symbol isolation, get_l1_scaled,
apply_update_with_stats_fields stale rejection, rapid updates,
get_book_snapshot for unknown symbol, and BookState edge cases.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, MetaData
from hft_platform.feed_adapter.lob_engine import BookState, LOBEngine


def _make_meta(ts: int = 0) -> MetaData:
    return MetaData(seq=1, topic="test", source_ts=ts, local_ts=time.time_ns())


def _np_bids(rows: list[list[int]]) -> np.ndarray:
    return np.array(rows, dtype=np.int64).reshape(-1, 2)


def _np_asks(rows: list[list[int]]) -> np.ndarray:
    return np.array(rows, dtype=np.int64).reshape(-1, 2)


def _empty_np() -> np.ndarray:
    """Return a fresh empty (0, 2) int64 array (avoids shared mutable state)."""
    return np.empty((0, 2), dtype=np.int64)


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("HFT_RUST_LOB", "0")
    return LOBEngine()


# ── 1. Single-sided book: bids only, no asks ────────────────────────────


class TestSingleSidedBookBidsOnly:
    def test_bids_only_numpy(self, engine):
        """All bids, no asks (numpy) -> mid_price_x2/spread = 0, no crash."""
        bids = _np_bids([[4_990_000, 10], [4_980_000, 20]])
        event = BidAskEvent(
            meta=_make_meta(1000),
            symbol="2330",
            bids=bids,
            asks=_empty_np(),
            is_snapshot=True,
        )
        stats = engine.process_event(event)
        assert stats is not None

        book = engine.get_book("2330")
        assert book.mid_price_x2 == 0
        assert book.spread == 0
        assert book.imbalance == 0.0
        assert book.bid_depth_total == 30  # 10 + 20
        assert book.ask_depth_total == 0

    def test_bids_only_list(self, engine):
        """All bids, no asks (plain list) -> mid_price_x2/spread = 0."""
        event = BidAskEvent(
            meta=_make_meta(1000),
            symbol="2330",
            bids=[[5_000_000, 10]],
            asks=[],
            is_snapshot=True,
        )
        stats = engine.process_event(event)
        assert stats is not None
        assert stats.mid_price_x2 == 0
        assert stats.spread_scaled == 0


# ── 2. Single-sided book: asks only, no bids ────────────────────────────


class TestSingleSidedBookAsksOnly:
    def test_asks_only_numpy(self, engine):
        """All asks, no bids (numpy) -> mid_price_x2/spread = 0."""
        asks = _np_asks([[5_010_000, 15], [5_020_000, 25]])
        event = BidAskEvent(
            meta=_make_meta(1000),
            symbol="2330",
            bids=_empty_np(),
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

    def test_asks_only_list(self, engine):
        """All asks, no bids (plain list) -> mid_price_x2/spread = 0."""
        event = BidAskEvent(
            meta=_make_meta(1000),
            symbol="2330",
            bids=[],
            asks=[[5_010_000, 20]],
            is_snapshot=True,
        )
        stats = engine.process_event(event)
        assert stats is not None
        assert stats.mid_price_x2 == 0
        assert stats.spread_scaled == 0


# ── 3. Multi-symbol coexistence ──────────────────────────────────────────


class TestMultiSymbolCoexistence:
    def test_two_symbols_independent_state(self, engine):
        """Two symbols in the same engine maintain fully isolated book state."""
        bids_a = _np_bids([[1_000_000, 5]])
        asks_a = _np_asks([[1_010_000, 10]])
        bids_b = _np_bids([[2_000_000, 20]])
        asks_b = _np_asks([[2_020_000, 30]])

        stats_a = engine.process_event(BidAskEvent(meta=_make_meta(100), symbol="AAAA", bids=bids_a, asks=asks_a))
        stats_b = engine.process_event(BidAskEvent(meta=_make_meta(200), symbol="BBBB", bids=bids_b, asks=asks_b))

        # Symbol A values
        assert stats_a.symbol == "AAAA"
        assert stats_a.best_bid == 1_000_000
        assert stats_a.best_ask == 1_010_000
        assert stats_a.mid_price_x2 == 2_010_000
        assert stats_a.bid_depth == 5
        assert stats_a.ask_depth == 10

        # Symbol B values
        assert stats_b.symbol == "BBBB"
        assert stats_b.best_bid == 2_000_000
        assert stats_b.best_ask == 2_020_000
        assert stats_b.mid_price_x2 == 4_020_000
        assert stats_b.bid_depth == 20
        assert stats_b.ask_depth == 30

    def test_update_one_symbol_does_not_affect_other(self, engine):
        """Updating symbol A must not mutate symbol B."""
        engine.process_event(
            BidAskEvent(
                meta=_make_meta(100),
                symbol="AAAA",
                bids=_np_bids([[1_000_000, 5]]),
                asks=_np_asks([[1_010_000, 10]]),
            )
        )
        engine.process_event(
            BidAskEvent(
                meta=_make_meta(100),
                symbol="BBBB",
                bids=_np_bids([[2_000_000, 20]]),
                asks=_np_asks([[2_020_000, 30]]),
            )
        )

        # Update only AAAA
        engine.process_event(
            BidAskEvent(
                meta=_make_meta(300),
                symbol="AAAA",
                bids=_np_bids([[1_050_000, 99]]),
                asks=_np_asks([[1_060_000, 99]]),
            )
        )

        book_a = engine.get_book("AAAA")
        book_b = engine.get_book("BBBB")
        assert book_a.mid_price_x2 == 1_050_000 + 1_060_000
        assert book_b.mid_price_x2 == 4_020_000  # unchanged


# ── 4. get_l1_scaled() direct test ───────────────────────────────────────


class TestGetL1Scaled:
    def test_returns_correct_values(self, engine):
        """get_l1_scaled returns (ts, best_bid, best_ask, mid_x2, spread, bid_depth, ask_depth)."""
        bids = _np_bids([[5_000_000, 10], [4_990_000, 5]])
        asks = _np_asks([[5_010_000, 20], [5_020_000, 15]])
        engine.process_event(BidAskEvent(meta=_make_meta(42000), symbol="2330", bids=bids, asks=asks))

        result = engine.get_l1_scaled("2330")
        assert result is not None
        ts, best_bid, best_ask, mid_x2, spread, bid_depth, ask_depth = result
        assert ts == 42000
        assert best_bid == 5_000_000
        assert best_ask == 5_010_000
        assert mid_x2 == 10_010_000  # 5_000_000 + 5_010_000
        assert spread == 10_000  # 5_010_000 - 5_000_000
        assert bid_depth == 15  # 10 + 5
        assert ask_depth == 35  # 20 + 15

    def test_returns_none_for_unknown_symbol(self, engine):
        """get_l1_scaled for unregistered symbol returns None."""
        assert engine.get_l1_scaled("UNKNOWN") is None

    def test_after_empty_book(self, engine):
        """get_l1_scaled after clearing book returns zeros for prices."""
        engine.process_event(
            BidAskEvent(
                meta=_make_meta(1000),
                symbol="2330",
                bids=_np_bids([[1_000_000, 10]]),
                asks=_np_asks([[1_010_000, 20]]),
            )
        )
        # Clear the book
        engine.process_event(
            BidAskEvent(
                meta=_make_meta(2000),
                symbol="2330",
                bids=_empty_np(),
                asks=_empty_np(),
            )
        )
        result = engine.get_l1_scaled("2330")
        assert result is not None
        _, best_bid, best_ask, mid_x2, spread, _, _ = result
        assert best_bid == 0
        assert best_ask == 0
        assert mid_x2 == 0
        assert spread == 0


# ── 5. apply_update_with_stats_fields() stale rejection ──────────────────


class TestApplyUpdateWithStatsFieldsStaleRejection:
    def test_stale_timestamp_rejected(self):
        """apply_update_with_stats_fields rejects update when exch_ts < current."""
        book = BookState("TEST")
        bids = _np_bids([[1_000_000, 10]])
        asks = _np_asks([[1_010_000, 20]])

        # First update at ts=2000
        book.apply_update_with_stats_fields(
            bids,
            asks,
            2000,
            best_bid=1_000_000,
            best_ask=1_010_000,
            bid_depth=10,
            ask_depth=20,
            _mid_price=0.0,
            _spread=0.0,
            imbalance=-0.333,
        )
        assert book.exch_ts == 2000
        assert book.version == 1

        # Stale update at ts=1000 -- should be silently rejected
        stale_bids = _np_bids([[500_000, 5]])
        stale_asks = _np_asks([[510_000, 5]])
        book.apply_update_with_stats_fields(
            stale_bids,
            stale_asks,
            1000,
            best_bid=500_000,
            best_ask=510_000,
            bid_depth=5,
            ask_depth=5,
            _mid_price=0.0,
            _spread=0.0,
            imbalance=0.0,
        )

        # State unchanged
        assert book.exch_ts == 2000
        assert book.version == 1
        assert book.mid_price_x2 == 1_000_000 + 1_010_000

    def test_equal_timestamp_accepted(self):
        """apply_update_with_stats_fields accepts update when exch_ts == current."""
        book = BookState("TEST")
        bids = _np_bids([[1_000_000, 10]])
        asks = _np_asks([[1_010_000, 20]])
        book.apply_update_with_stats_fields(
            bids,
            asks,
            2000,
            best_bid=1_000_000,
            best_ask=1_010_000,
            bid_depth=10,
            ask_depth=20,
            _mid_price=0.0,
            _spread=0.0,
            imbalance=0.0,
        )

        # Same timestamp with different data -- should be accepted (not strictly less)
        new_bids = _np_bids([[1_050_000, 15]])
        new_asks = _np_asks([[1_060_000, 25]])
        book.apply_update_with_stats_fields(
            new_bids,
            new_asks,
            2000,
            best_bid=1_050_000,
            best_ask=1_060_000,
            bid_depth=15,
            ask_depth=25,
            _mid_price=0.0,
            _spread=0.0,
            imbalance=0.1,
        )

        assert book.version == 2
        assert book.mid_price_x2 == 1_050_000 + 1_060_000


# ── 6. Rapid updates to same symbol ─────────────────────────────────────


class TestRapidUpdates:
    def test_100_rapid_updates_no_corruption(self, engine):
        """100 sequential BidAsk updates maintain consistent state."""
        symbol = "2330"
        for i in range(100):
            bid_price = 5_000_000 + i * 1000
            ask_price = 5_010_000 + i * 1000
            bid_vol = 10 + i
            ask_vol = 20 + i
            engine.process_event(
                BidAskEvent(
                    meta=_make_meta(1000 + i),
                    symbol=symbol,
                    bids=_np_bids([[bid_price, bid_vol]]),
                    asks=_np_asks([[ask_price, ask_vol]]),
                )
            )

        book = engine.get_book(symbol)
        expected_bid = 5_000_000 + 99 * 1000
        expected_ask = 5_010_000 + 99 * 1000
        assert book.exch_ts == 1099
        assert book.mid_price_x2 == expected_bid + expected_ask
        assert book.spread == expected_ask - expected_bid  # always 10_000
        assert book.bid_depth_total == 10 + 99
        assert book.ask_depth_total == 20 + 99
        assert book.version == 100

    def test_rapid_updates_interleaved_symbols(self, engine):
        """Alternating updates between two symbols both remain correct."""
        for i in range(50):
            engine.process_event(
                BidAskEvent(
                    meta=_make_meta(1000 + i),
                    symbol="A",
                    bids=_np_bids([[1_000_000 + i, 10]]),
                    asks=_np_asks([[1_010_000 + i, 20]]),
                )
            )
            engine.process_event(
                BidAskEvent(
                    meta=_make_meta(1000 + i),
                    symbol="B",
                    bids=_np_bids([[2_000_000 + i, 30]]),
                    asks=_np_asks([[2_020_000 + i, 40]]),
                )
            )

        book_a = engine.get_book("A")
        book_b = engine.get_book("B")
        assert book_a.mid_price_x2 == (1_000_000 + 49) + (1_010_000 + 49)
        assert book_b.mid_price_x2 == (2_000_000 + 49) + (2_020_000 + 49)


# ── 7. get_book_snapshot() for unknown symbol ────────────────────────────


class TestGetBookSnapshotUnknown:
    def test_unknown_symbol_returns_none(self, engine):
        """get_book_snapshot for symbol never seen returns None."""
        assert engine.get_book_snapshot("DOES_NOT_EXIST") is None

    def test_known_symbol_returns_valid_dict(self, engine):
        """get_book_snapshot for known symbol returns dict with correct fields."""
        engine.process_event(
            BidAskEvent(
                meta=_make_meta(1000),
                symbol="2330",
                bids=_np_bids([[1_000_000, 10]]),
                asks=_np_asks([[1_010_000, 20]]),
            )
        )
        snap = engine.get_book_snapshot("2330")
        assert snap is not None
        assert snap["symbol"] == "2330"
        assert snap["best_bid"] == 1_000_000
        assert snap["best_ask"] == 1_010_000
        assert snap["mid_price_x2"] == 2_010_000
        assert snap["spread_scaled"] == 10_000


# ── BookState edge cases ─────────────────────────────────────────────────


class TestBookStateEdges:
    def test_initial_state_zeros(self):
        """New BookState starts with all stats at zero."""
        book = BookState("TEST")
        assert book.exch_ts == 0
        assert book.version == 0
        assert book.mid_price_x2 == 0
        assert book.spread == 0
        assert book.imbalance == 0.0
        assert book.bid_depth_total == 0
        assert book.ask_depth_total == 0
        assert book.last_price == 0
        assert book.last_volume == 0

    def test_get_stats_on_empty_book(self):
        """get_stats on empty book returns LOBStatsEvent with zeros."""
        book = BookState("TEST")
        stats = book.get_stats()
        assert stats.symbol == "TEST"
        assert stats.best_bid == 0
        assert stats.best_ask == 0
        assert stats.mid_price_x2 == 0
        assert stats.spread_scaled == 0

    def test_get_stats_tuple_on_empty_book(self):
        """get_stats_tuple on empty book returns tuple with zeros."""
        book = BookState("TEST")
        t = book.get_stats_tuple()
        assert t[0] == "TEST"  # symbol
        assert t[5] == 0  # best_bid
        assert t[6] == 0  # best_ask

    def test_imbalance_equal_volumes(self):
        """Equal bid/ask top volumes -> imbalance = 0."""
        bids = _np_bids([[5_000_000, 50]])
        asks = _np_asks([[5_010_000, 50]])
        event = BidAskEvent(meta=_make_meta(1000), symbol="SYM", bids=bids, asks=asks)
        eng = LOBEngine()
        stats = eng.process_event(event)
        assert stats.imbalance == pytest.approx(0.0)

    def test_imbalance_known_ratio(self):
        """Known bid/ask volumes -> imbalance = (bid - ask) / (bid + ask)."""
        bid_vol, ask_vol = 30, 10
        expected = (bid_vol - ask_vol) / (bid_vol + ask_vol)  # 0.5

        bids = _np_bids([[5_000_000, bid_vol]])
        asks = _np_asks([[5_010_000, ask_vol]])
        event = BidAskEvent(meta=_make_meta(1000), symbol="SYM", bids=bids, asks=asks)
        eng = LOBEngine()
        stats = eng.process_event(event)
        assert stats.imbalance == pytest.approx(expected)
