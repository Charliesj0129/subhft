import time

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, BookStats, FusedBookStats, MetaData, TickEvent
from hft_platform.feed_adapter.lob_engine import LOBEngine


@pytest.fixture
def engine():
    return LOBEngine()


def make_meta(ts=0):
    return MetaData(seq=1, topic="test", source_ts=ts, local_ts=time.time_ns())


def test_bidask_update(engine):
    bids = np.array([[5000000, 10]], dtype=np.int64)
    asks = np.array([[5010000, 20]], dtype=np.int64)
    event = BidAskEvent(meta=make_meta(1000), symbol="2330", bids=bids, asks=asks, is_snapshot=True)

    stats = engine.process_event(event)
    assert stats.symbol == "2330"
    assert stats.best_bid == 5000000
    assert stats.best_ask == 5010000
    assert stats.mid_price_x2 == 10010000  # best_bid + best_ask
    assert stats.spread_scaled == 10000  # best_ask - best_bid
    assert stats.bid_depth == 10
    assert stats.ask_depth == 20
    assert stats.imbalance == pytest.approx(-0.3333333)


def test_incremental_update(engine):
    engine.process_event(
        BidAskEvent(
            meta=make_meta(1000),
            symbol="2330",
            bids=np.array([[100, 10]], dtype=np.int64),
            asks=np.array([[102, 10]], dtype=np.int64),
        )
    )
    event = BidAskEvent(
        meta=make_meta(1001),
        symbol="2330",
        bids=np.array([[101, 5]], dtype=np.int64),
        asks=np.array([[102, 10]], dtype=np.int64),
    )
    stats = engine.process_event(event)

    assert stats.best_bid == 101
    assert stats.mid_price_x2 == 203  # 101 + 102
    assert stats.spread_scaled == 1  # 102 - 101
    assert stats.bid_depth == 5


def test_tick_update(engine):
    engine.process_event(
        BidAskEvent(
            meta=make_meta(1000),
            symbol="2330",
            bids=np.array([[100, 10]], dtype=np.int64),
            asks=np.array([[102, 10]], dtype=np.int64),
        )
    )
    tick = TickEvent(
        meta=make_meta(1005),
        symbol="2330",
        price=101,
        volume=2,
        total_volume=100,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )
    stats = engine.process_event(tick)
    assert stats is None
    book = engine.get_book("2330")
    assert book.last_price == 101


def test_missing_symbol_snapshot(engine):
    snap = engine.get_book_snapshot("UNKNOWN")
    assert snap is None


def test_valid_snapshot(engine):
    engine.process_event(
        BidAskEvent(
            meta=make_meta(1000),
            symbol="2330",
            bids=np.array([[100, 10]], dtype=np.int64),
            asks=np.array([[102, 10]], dtype=np.int64),
        )
    )
    snap = engine.get_book_snapshot("2330")
    assert snap is not None
    assert snap["symbol"] == "2330"
    assert snap["best_bid"] == 100
    assert snap["best_ask"] == 102


def test_empty_update(engine):
    # Should clear book if empty list passed? Or just not crash?
    # Code says: if bids: self.bids = bids; else: self.bids = []
    # So it clears.
    engine.process_event(BidAskEvent(meta=make_meta(1000), symbol="2330", bids=[[100, 1]], asks=[[102, 1]]))
    stats = engine.process_event(BidAskEvent(meta=make_meta(1001), symbol="2330", bids=[], asks=[]))
    assert stats.mid_price == 0.0  # Empty book, backward-compat field
    assert stats.bid_depth == 0


def test_late_packet_handling(engine):
    # 1. Update with ts=2000
    engine.process_event(BidAskEvent(meta=make_meta(2000), symbol="2330", bids=[[100, 1]], asks=[[102, 1]]))
    book = engine.get_book("2330")
    assert book.exch_ts == 2000
    assert book.mid_price_x2 / 2.0 == 101.0

    # 2. Late update ts=1000 with DIFFERENT price
    # Should be IGNORED
    engine.process_event(BidAskEvent(meta=make_meta(1000), symbol="2330", bids=[[50, 1]], asks=[[52, 1]]))

    # 3. Verify state UNCHANGED
    assert book.exch_ts == 2000
    assert book.mid_price_x2 / 2.0 == 101.0  # If bug exists, this might fail (be 51.0)


def test_stats_tuple_mode_equivalent_to_event_mode(engine):
    """Stats tuple output should contain the same data as LOBStatsEvent output."""
    bids = np.array([[5000000, 10]], dtype=np.int64)
    asks = np.array([[5010000, 20]], dtype=np.int64)
    event = BidAskEvent(meta=make_meta(1000), symbol="2330", bids=bids, asks=asks, is_snapshot=True)

    # Get event-mode stats
    stats_event = engine.process_event(event)
    assert stats_event is not None

    # Get tuple-mode stats via get_stats_tuple
    book = engine.get_book("2330")
    stats_tuple = book.get_stats_tuple()
    assert isinstance(stats_tuple, tuple)
    assert len(stats_tuple) == 10

    # Verify fields match: ("lobstats", symbol, ts, mid_price_x2, spread, imbalance, best_bid, best_ask, bid_depth, ask_depth)
    assert stats_tuple[0] == "lobstats"
    assert stats_tuple[1] == stats_event.symbol
    assert stats_tuple[2] == stats_event.ts
    assert stats_tuple[3] == stats_event.mid_price_x2
    assert stats_tuple[4] == stats_event.spread_scaled
    assert stats_tuple[5] == pytest.approx(stats_event.imbalance)
    assert stats_tuple[6] == stats_event.best_bid
    assert stats_tuple[7] == stats_event.best_ask
    assert stats_tuple[8] == stats_event.bid_depth
    assert stats_tuple[9] == stats_event.ask_depth


def test_stats_tuple_mode_via_emit_stats(engine):
    """When _STATS_TUPLE is true, _emit_stats returns a tuple."""
    from hft_platform.feed_adapter import lob_engine as _mod

    orig = _mod._STATS_TUPLE
    try:
        _mod._STATS_TUPLE = True
        bids = np.array([[5000000, 10]], dtype=np.int64)
        asks = np.array([[5010000, 20]], dtype=np.int64)
        event = BidAskEvent(meta=make_meta(1000), symbol="2330", bids=bids, asks=asks, is_snapshot=True)
        result = engine.process_event(event)
        assert isinstance(result, tuple)
        assert result[0] == "lobstats"
        assert result[1] == "2330"
    finally:
        _mod._STATS_TUPLE = orig


from unittest.mock import MagicMock


def test_tick_late(engine):
    engine.process_event(BidAskEvent(meta=make_meta(1000), symbol="A", bids=[[100, 10]], asks=[[102, 10]]))
    # Late tick
    engine.process_event(
        TickEvent(
            meta=make_meta(900),
            symbol="A",
            price=99,
            volume=1,
            total_volume=0,
            bid_side_total_vol=0,
            ask_side_total_vol=0,
            is_simtrade=False,
            is_odd_lot=False,
        )
    )

    book = engine.get_book("A")
    assert book.last_price == 0  # Should not update


def test_metrics_emit(engine):
    engine.metrics = MagicMock()
    engine.process_event(BidAskEvent(meta=make_meta(1001), symbol="A", bids=[[100, 1]], asks=[[101, 1]]))
    # Verify metric call
    # self.metrics.lob_updates_total.labels(symbol=..., type="BidAsk").inc()
    engine.metrics.lob_updates_total.labels.assert_called()
    engine.metrics.lob_updates_total.labels.return_value.inc.assert_called()


# --- Fused bypass tests ---


def test_fused_bypass_sets_book_stats(engine, monkeypatch):
    """When _FUSED_BYPASS is on and fused_stats present, LOBEngine should use bypass path."""
    import hft_platform.feed_adapter.lob_engine as lob_mod

    monkeypatch.setattr(lob_mod, "_FUSED_BYPASS", True)

    bids = np.array([[1000000, 10]], dtype=np.int64)
    asks = np.array([[1005000, 20]], dtype=np.int64)
    # fused_stats: (best_bid, best_ask, bid_depth, ask_depth, mid_x2, spread_scaled, imbalance)
    fused_stats = FusedBookStats(1000000, 1005000, 10, 20, 2005000, 5000, -0.333333)

    event = BidAskEvent(
        meta=make_meta(1000),
        symbol="2330",
        bids=bids,
        asks=asks,
        fused_stats=fused_stats,
    )

    stats = engine.process_event(event)
    assert stats is not None
    assert stats.symbol == "2330"
    assert stats.best_bid == 1000000
    assert stats.best_ask == 1005000
    assert stats.mid_price_x2 == 2005000
    assert stats.spread_scaled == 5000
    assert stats.bid_depth == 10
    assert stats.ask_depth == 20
    assert stats.imbalance == pytest.approx(-0.333333)

    # Verify BookState was updated
    book = engine.get_book("2330")
    assert book.mid_price_x2 == 2005000
    assert book.spread == 5000
    assert book.version == 1


def test_fused_bypass_respects_late_packet(engine, monkeypatch):
    """Fused bypass should still reject late packets."""
    import hft_platform.feed_adapter.lob_engine as lob_mod

    monkeypatch.setattr(lob_mod, "_FUSED_BYPASS", True)

    fused_stats = FusedBookStats(1000000, 1005000, 10, 20, 2005000, 5000, -0.333333)

    # First event at ts=2000
    event1 = BidAskEvent(
        meta=make_meta(2000),
        symbol="2330",
        bids=np.array([[1000000, 10]], dtype=np.int64),
        asks=np.array([[1005000, 20]], dtype=np.int64),
        fused_stats=fused_stats,
    )
    engine.process_event(event1)

    # Late event at ts=1000 with different stats
    late_fused = (500000, 505000, 5, 5, 1005000, 5000, 0.0)
    event2 = BidAskEvent(
        meta=make_meta(1000),
        symbol="2330",
        bids=np.array([[500000, 5]], dtype=np.int64),
        asks=np.array([[505000, 5]], dtype=np.int64),
        fused_stats=late_fused,
    )
    engine.process_event(event2)

    # Book should retain first event's values
    book = engine.get_book("2330")
    assert book.mid_price_x2 == 2005000
    assert book.exch_ts == 2000


def test_fused_bypass_produces_same_stats_as_standard(engine):
    """Fused bypass and standard path should produce equivalent LOBStatsEvent."""
    import hft_platform.feed_adapter.lob_engine as lob_mod

    bids = np.array([[1000000, 10]], dtype=np.int64)
    asks = np.array([[1005000, 20]], dtype=np.int64)

    # Standard path
    event_std = BidAskEvent(meta=make_meta(1000), symbol="STD", bids=bids.copy(), asks=asks.copy())
    stats_std = engine.process_event(event_std)

    # Fused bypass path
    engine2 = LOBEngine()
    original_val = lob_mod._FUSED_BYPASS
    try:
        lob_mod._FUSED_BYPASS = True
        fused_stats = FusedBookStats(1000000, 1005000, 10, 20, 2005000, 5000, stats_std.imbalance)
        event_fused = BidAskEvent(
            meta=make_meta(1000),
            symbol="FUSED",
            bids=bids.copy(),
            asks=asks.copy(),
            fused_stats=fused_stats,
        )
        stats_fused = engine2.process_event(event_fused)
    finally:
        lob_mod._FUSED_BYPASS = original_val

    assert stats_fused.best_bid == stats_std.best_bid
    assert stats_fused.best_ask == stats_std.best_ask
    assert stats_fused.mid_price_x2 == stats_std.mid_price_x2
    assert stats_fused.spread_scaled == stats_std.spread_scaled
    assert stats_fused.bid_depth == stats_std.bid_depth
    assert stats_fused.ask_depth == stats_std.ask_depth
    assert stats_fused.imbalance == pytest.approx(stats_std.imbalance)


def test_no_fused_bypass_when_flag_off(engine):
    """Without _FUSED_BYPASS, fused_stats on event should be ignored."""
    fused_stats = FusedBookStats(1000000, 1005000, 10, 20, 2005000, 5000, -0.333333)
    bids = np.array([[1000000, 10]], dtype=np.int64)
    asks = np.array([[1005000, 20]], dtype=np.int64)

    event = BidAskEvent(
        meta=make_meta(1000),
        symbol="2330",
        bids=bids,
        asks=asks,
        fused_stats=fused_stats,
    )
    stats = engine.process_event(event)

    # Should still produce valid stats (via standard apply_update path)
    assert stats is not None
    assert stats.best_bid == 1000000


# --- NamedTuple contract tests ---


def test_bookstats_named_field_access():
    """BookStats supports both named-field and integer-index access (NamedTuple contract)."""
    bs = BookStats(
        best_bid=1000000,
        best_ask=1005000,
        bid_depth=10,
        ask_depth=20,
        mid_price=1002500.0,
        spread=5000.0,
        imbalance=-0.333,
    )
    # Named access
    assert bs.best_bid == 1000000
    assert bs.best_ask == 1005000
    assert bs.bid_depth == 10
    assert bs.ask_depth == 20
    assert bs.mid_price == pytest.approx(1002500.0)
    assert bs.spread == pytest.approx(5000.0)
    assert bs.imbalance == pytest.approx(-0.333)
    # Backward-compat index access
    assert bs[0] == 1000000
    assert bs[4] == pytest.approx(1002500.0)
    # Unpacking still works
    bb, ba, bd, ad, mp, sp, imb = bs
    assert bb == 1000000 and ba == 1005000


def test_fusedbookstats_named_field_access():
    """FusedBookStats supports both named-field and integer-index access (NamedTuple contract)."""
    fs = FusedBookStats(
        best_bid=1000000,
        best_ask=1005000,
        bid_depth=10,
        ask_depth=20,
        mid_price_x2=2005000,
        spread_scaled=5000,
        imbalance=-0.333,
    )
    # Named access
    assert fs.best_bid == 1000000
    assert fs.mid_price_x2 == 2005000
    assert fs.spread_scaled == 5000
    assert fs.imbalance == pytest.approx(-0.333)
    # Backward-compat index access
    assert fs[2] == 10   # bid_depth
    assert fs[4] == 2005000  # mid_price_x2
    assert fs[5] == 5000  # spread_scaled
    # Unpacking still works
    bb, ba, bd, ad, mx2, ss, imb = fs
    assert mx2 == 2005000 and ss == 5000


def test_bidaskevent_stats_are_named_tuples():
    """BidAskEvent.stats and .fused_stats are NamedTuple instances, not plain tuples."""
    meta = make_meta(0)
    bids = np.array([[1000000, 10]], dtype=np.int64)
    asks = np.array([[1005000, 20]], dtype=np.int64)
    bs = BookStats(1000000, 1005000, 10, 20, 1002500.0, 5000.0, -0.333)
    fs = FusedBookStats(1000000, 1005000, 10, 20, 2005000, 5000, -0.333)
    event = BidAskEvent(meta=meta, symbol="2330", bids=bids, asks=asks, stats=bs, fused_stats=fs)
    assert isinstance(event.stats, BookStats)
    assert isinstance(event.fused_stats, FusedBookStats)
    assert event.stats.best_bid == 1000000
    assert event.fused_stats.mid_price_x2 == 2005000


class TestSymbolCardinalityGuard:
    """Rule 12: symbol cardinality guard prevents unbounded dict growth."""

    def test_get_book_returns_none_when_limit_exceeded(self):
        engine = LOBEngine()
        engine._max_symbols = 2
        # Fill up to limit
        assert engine.get_book("SYM_A") is not None
        assert engine.get_book("SYM_B") is not None
        # Third symbol should be rejected
        assert engine.get_book("SYM_C") is None
        assert len(engine.books) == 2

    def test_get_book_allows_existing_symbol_at_limit(self):
        engine = LOBEngine()
        engine._max_symbols = 2
        engine.get_book("SYM_A")
        engine.get_book("SYM_B")
        # Existing symbol still accessible
        book = engine.get_book("SYM_A")
        assert book is not None
        assert book.symbol == "SYM_A"

    def test_process_event_returns_none_for_rejected_symbol(self):
        engine = LOBEngine()
        engine._max_symbols = 1
        # Fill the single slot
        bids = np.array([[5000000, 10]], dtype=np.int64)
        asks = np.array([[5010000, 20]], dtype=np.int64)
        event1 = BidAskEvent(
            meta=make_meta(1000), symbol="SYM_A", bids=bids, asks=asks, is_snapshot=True
        )
        result = engine.process_event(event1)
        assert result is not None
        # Second symbol should be silently skipped
        event2 = BidAskEvent(
            meta=make_meta(2000), symbol="SYM_B", bids=bids, asks=asks, is_snapshot=True
        )
        result = engine.process_event(event2)
        assert result is None

    def test_cardinality_warning_logged(self):
        from unittest.mock import patch, MagicMock

        engine = LOBEngine()
        engine._max_symbols = 0  # reject all new symbols
        mock_logger = MagicMock()
        with patch("hft_platform.feed_adapter.lob_engine.logger", mock_logger):
            result = engine.get_book("SYM_X")
        assert result is None
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "lob_symbol_cardinality_exceeded"

    def test_default_max_symbols_is_10000(self):
        engine = LOBEngine()
        assert engine._max_symbols == 10000


class TestApplyUpdateWithStatsFieldsCrossedBookGuard:
    """Verify apply_update_with_stats_fields rejects crossed-book inputs."""

    def _make_book(self) -> "BookState":
        from hft_platform.feed_adapter.lob_engine import BookState

        return BookState("TEST")

    def test_crossed_book_zeros_stats(self):
        book = self._make_book()
        bids = np.array([[5010000, 10]], dtype=np.int64)
        asks = np.array([[5000000, 20]], dtype=np.int64)
        # best_bid (5010000) > best_ask (5000000) — crossed book
        book.apply_update_with_stats_fields(
            bids, asks, exch_ts=1000,
            best_bid=5010000, best_ask=5000000,
            bid_depth=10, ask_depth=20,
            _mid_price=0.0, _spread=0.0, imbalance=0.5,
        )
        assert book.mid_price_x2 == 0
        assert book.spread == 0
        assert book.imbalance == 0.0

    def test_zero_bid_zeros_stats(self):
        book = self._make_book()
        bids = np.array([], dtype=np.int64).reshape(0, 2)
        asks = np.array([[5000000, 20]], dtype=np.int64)
        book.apply_update_with_stats_fields(
            bids, asks, exch_ts=1000,
            best_bid=0, best_ask=5000000,
            bid_depth=0, ask_depth=20,
            _mid_price=0.0, _spread=0.0, imbalance=0.0,
        )
        assert book.mid_price_x2 == 0
        assert book.spread == 0
        assert book.imbalance == 0.0

    def test_normal_book_propagates_stats(self):
        book = self._make_book()
        bids = np.array([[5000000, 10]], dtype=np.int64)
        asks = np.array([[5010000, 20]], dtype=np.int64)
        book.apply_update_with_stats_fields(
            bids, asks, exch_ts=1000,
            best_bid=5000000, best_ask=5010000,
            bid_depth=10, ask_depth=20,
            _mid_price=5005000.0, _spread=10000.0, imbalance=-0.333,
        )
        assert book.mid_price_x2 == 10010000
        assert book.spread == 10000
        assert book.imbalance == pytest.approx(-0.333)
