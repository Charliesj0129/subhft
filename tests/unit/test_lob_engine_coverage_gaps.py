"""Coverage gap tests for feed_adapter/lob_engine.py.

Targets uncovered branches: Python _recompute fallback with lists,
empty arrays, crossed-book guard, stats mode variants, get_l1_scaled
fallback paths, LOBEngine cardinality guard, eviction, and metrics
flushing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from hft_platform.events import BidAskEvent, BookStats, LOBStatsEvent, MetaData, TickEvent
from hft_platform.feed_adapter.lob_engine import BookState, LOBEngine, _NoopLock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _np_bids():
    return np.array([[1000000, 10], [999000, 5]], dtype=np.int64)


def _np_asks():
    return np.array([[1001000, 8], [1002000, 3]], dtype=np.int64)


def _list_bids():
    return [[1000000, 10], [999000, 5]]


def _list_asks():
    return [[1001000, 8], [1002000, 3]]


def _make_meta(seq=1, ts=1_000_000_000):
    return MetaData(seq=seq, source_ts=ts, local_ts=ts)


# ---------------------------------------------------------------------------
# BookState: _recompute with pure-Python list data
# ---------------------------------------------------------------------------


class TestBookStateRecompute:
    def test_recompute_with_lists(self):
        """Pure-Python _recompute path when bids/asks are lists."""
        book = BookState("TST")
        book._rust_state = None  # Disable Rust
        book.bids = _list_bids()
        book.asks = _list_asks()
        book._recompute()
        assert book.mid_price_x2 == 1000000 + 1001000
        assert book.spread == 1001000 - 1000000
        assert book.bid_depth_total == 15  # 10+5
        assert book.ask_depth_total == 11  # 8+3

    def test_recompute_with_empty_lists(self):
        book = BookState("TST")
        book._rust_state = None
        book.bids = []
        book.asks = []
        book._recompute()
        assert book.mid_price_x2 == 0
        assert book.spread == 0

    def test_recompute_with_empty_numpy(self):
        book = BookState("TST")
        book._rust_state = None
        book.bids = np.empty((0, 2), dtype=np.int64)
        book.asks = np.empty((0, 2), dtype=np.int64)
        book._recompute()
        assert book.mid_price_x2 == 0
        assert book.spread == 0

    def test_recompute_crossed_book_zeroes_stats(self):
        """When best_bid > best_ask (crossed book), stats are zeroed."""
        book = BookState("TST")
        book._rust_state = None
        # Crossed: bid > ask
        book.bids = [[1010000, 10]]
        book.asks = [[1000000, 8]]
        book._recompute()
        assert book.mid_price_x2 == 0
        assert book.spread == 0
        assert book.imbalance == 0.0

    def test_recompute_zero_top_volume(self):
        """When top level volumes are both zero, imbalance is 0."""
        book = BookState("TST")
        book._rust_state = None
        book.bids = [[1000000, 0]]
        book.asks = [[1001000, 0]]
        book._recompute()
        assert book.imbalance == 0.0

    def test_recompute_one_sided_book(self):
        """Only bids, no asks."""
        book = BookState("TST")
        book._rust_state = None
        book.bids = _list_bids()
        book.asks = []
        book._recompute()
        assert book.mid_price_x2 == 0
        assert book.bid_depth_total == 15


# ---------------------------------------------------------------------------
# BookState: apply_update stale packet rejection
# ---------------------------------------------------------------------------


class TestBookStateApplyUpdate:
    def test_stale_packet_rejected(self):
        """apply_update skips updates with older exch_ts."""
        book = BookState("TST")
        book._rust_state = None
        book.apply_update(_np_bids(), _np_asks(), 2000)
        old_version = book.version
        book.apply_update(_np_bids(), _np_asks(), 1000)  # Stale
        assert book.version == old_version  # Not incremented

    def test_zero_ts_preserves_existing(self):
        """When new update has ts=0, existing valid ts is preserved."""
        book = BookState("TST")
        book._rust_state = None
        book.apply_update(_np_bids(), _np_asks(), 5000)
        assert book.exch_ts == 5000
        book.apply_update(_np_bids(), _np_asks(), 0)
        assert book.exch_ts == 5000  # Preserved

    def test_apply_update_empty_bids_clears(self):
        book = BookState("TST")
        book._rust_state = None
        book.apply_update(_np_bids(), _np_asks(), 1000)
        book.apply_update([], [], 2000)
        assert book.bids == []
        assert book.asks == []


# ---------------------------------------------------------------------------
# BookState: update_tick
# ---------------------------------------------------------------------------


class TestBookStateUpdateTick:
    def test_update_tick_stale_rejected(self):
        book = BookState("TST")
        book.update_tick(1000, 10, 2000)
        book.update_tick(999, 5, 1000)  # Stale
        assert book.last_price == 1000  # Not overwritten

    def test_update_tick_normal(self):
        book = BookState("TST")
        book.update_tick(5000, 100, 1000)
        assert book.last_price == 5000
        assert book.last_volume == 100


# ---------------------------------------------------------------------------
# BookState: get_stats with list data
# ---------------------------------------------------------------------------


class TestBookStateGetStats:
    def test_get_stats_with_lists(self):
        book = BookState("TST")
        book._rust_state = None
        book.bids = _list_bids()
        book.asks = _list_asks()
        book._recompute()
        stats = book.get_stats()
        assert isinstance(stats, LOBStatsEvent)
        assert stats.best_bid == 1000000
        assert stats.best_ask == 1001000

    def test_get_stats_empty(self):
        book = BookState("TST")
        book._rust_state = None
        stats = book.get_stats()
        assert stats.best_bid == 0
        assert stats.best_ask == 0


# ---------------------------------------------------------------------------
# BookState: get_stats_tuple
# ---------------------------------------------------------------------------


class TestBookStateGetStatsTuple:
    def test_get_stats_tuple_with_lists(self):
        book = BookState("TST")
        book._rust_state = None
        book.bids = _list_bids()
        book.asks = _list_asks()
        book._recompute()
        t = book.get_stats_tuple()
        assert t[0] == "lobstats"
        assert t[1] == "TST"

    def test_get_stats_tuple_empty(self):
        book = BookState("TST")
        book._rust_state = None
        t = book.get_stats_tuple()
        assert t[0] == "lobstats"
        assert t[6] == 0  # best_bid
        assert t[7] == 0  # best_ask


# ---------------------------------------------------------------------------
# BookState: apply_update_with_stats_fields
# ---------------------------------------------------------------------------


class TestBookStateApplyUpdateWithStatsFields:
    def test_apply_update_with_stats_fields_crossed(self):
        """Crossed book zeroes stats."""
        book = BookState("TST")
        book._rust_state = None
        book.apply_update_with_stats_fields(
            _list_bids(), _list_asks(), 1000,
            best_bid=1010000, best_ask=1000000,  # Crossed
            bid_depth=15, ask_depth=11,
            _mid_price=0.0, _spread=0.0,
            imbalance=0.1,
        )
        assert book.mid_price_x2 == 0
        assert book.spread == 0

    def test_apply_update_with_stats_stale_rejected(self):
        book = BookState("TST")
        book._rust_state = None
        book.apply_update_with_stats_fields(
            _list_bids(), _list_asks(), 2000,
            1000000, 1001000, 15, 11, 0.0, 0.0, 0.1,
        )
        v = book.version
        book.apply_update_with_stats_fields(
            _list_bids(), _list_asks(), 1000,  # Stale
            1000000, 1001000, 15, 11, 0.0, 0.0, 0.1,
        )
        assert book.version == v

    def test_apply_update_with_stats_fields_empty_data(self):
        book = BookState("TST")
        book._rust_state = None
        book.apply_update_with_stats_fields(
            [], [], 1000,
            1000000, 1001000, 0, 0, 0.0, 0.0, 0.0,
        )
        assert book.bids == []
        assert book.asks == []


# ---------------------------------------------------------------------------
# LOBEngine: get_book cardinality guard
# ---------------------------------------------------------------------------


class TestLOBEngineGetBook:
    def test_cardinality_guard_rejects_new_symbol(self):
        engine = LOBEngine()
        engine._max_symbols = 2
        engine.get_book("SYM1")
        engine.get_book("SYM2")
        result = engine.get_book("SYM3")
        assert result is None  # Rejected

    def test_get_book_cached(self):
        engine = LOBEngine()
        b1 = engine.get_book("TST")
        b2 = engine.get_book("TST")
        assert b1 is b2  # Cached


# ---------------------------------------------------------------------------
# LOBEngine: process_event tuple paths
# ---------------------------------------------------------------------------


class TestLOBEngineProcessEvent:
    def test_tuple_bidask_short_format(self):
        """Short tuple format (< 13 elements) uses apply_update."""
        engine = LOBEngine()
        bids = _np_bids()
        asks = _np_asks()
        event = ("bidask", "TST", bids, asks, 1000, False)
        result = engine.process_event(event)
        assert result is not None

    def test_tuple_bidask_long_format(self):
        """Long tuple format (>= 13 elements) uses apply_update_with_stats_fields."""
        engine = LOBEngine()
        bids = _np_bids()
        asks = _np_asks()
        event = (
            "bidask", "TST", bids, asks, 1000, False,
            1000000, 1001000, 15, 11, 1000500.0, 1000.0, 0.1,
        )
        result = engine.process_event(event)
        assert result is not None

    def test_tuple_tick(self):
        engine = LOBEngine()
        event = ("tick", "TST", 1000000, 100, 500, False, False, 1000)
        result = engine.process_event(event)
        assert result is None  # Ticks don't emit stats

    def test_tuple_unknown_tag(self):
        engine = LOBEngine()
        result = engine.process_event(("unknown_tag", "data"))
        assert result is None

    def test_tuple_bidask_cardinality_exceeded(self):
        engine = LOBEngine()
        engine._max_symbols = 0
        event = ("bidask", "TST", _np_bids(), _np_asks(), 1000, False)
        result = engine.process_event(event)
        assert result is None

    def test_tick_event_object(self):
        engine = LOBEngine()
        meta = _make_meta()
        event = TickEvent(meta=meta, symbol="TST", price=1000000, volume=10)
        result = engine.process_event(event)
        assert result is None

    def test_bidask_event_with_stats(self):
        engine = LOBEngine()
        meta = _make_meta()
        stats = BookStats(
            best_bid=1000000, best_ask=1001000,
            bid_depth=15, ask_depth=11,
            mid_price=1000500.0, spread=1000.0,
            imbalance=0.1,
        )
        event = BidAskEvent(
            meta=meta, symbol="TST",
            bids=_np_bids(), asks=_np_asks(),
            stats=stats,
        )
        result = engine.process_event(event)
        assert result is not None

    def test_bidask_event_no_stats(self):
        engine = LOBEngine()
        meta = _make_meta()
        event = BidAskEvent(
            meta=meta, symbol="TST",
            bids=_np_bids(), asks=_np_asks(),
            stats=None,
        )
        result = engine.process_event(event)
        assert result is not None

    def test_unknown_event_type(self):
        engine = LOBEngine()
        result = engine.process_event("not_an_event")
        assert result is None


# ---------------------------------------------------------------------------
# LOBEngine: get_book_snapshot
# ---------------------------------------------------------------------------


class TestLOBEngineGetBookSnapshot:
    def test_get_book_snapshot_with_data(self):
        engine = LOBEngine()
        book = engine.get_book("TST")
        book._rust_state = None
        book.apply_update(_np_bids(), _np_asks(), 1000)
        snap = engine.get_book_snapshot("TST")
        assert snap is not None
        assert snap["symbol"] == "TST"
        assert snap["best_bid"] == 1000000
        assert snap["best_ask"] == 1001000

    def test_get_book_snapshot_nonexistent(self):
        engine = LOBEngine()
        assert engine.get_book_snapshot("NONE") is None

    def test_get_book_snapshot_empty_book(self):
        engine = LOBEngine()
        engine.get_book("TST")
        snap = engine.get_book_snapshot("TST")
        assert snap is not None
        assert snap["best_bid"] == 0
        assert snap["best_ask"] == 0

    def test_get_book_snapshot_list_data(self):
        engine = LOBEngine()
        book = engine.get_book("TST")
        book._rust_state = None
        book.bids = _list_bids()
        book.asks = _list_asks()
        snap = engine.get_book_snapshot("TST")
        assert snap["best_bid"] == 1000000


# ---------------------------------------------------------------------------
# LOBEngine: get_l1_scaled
# ---------------------------------------------------------------------------


class TestLOBEngineGetL1Scaled:
    def test_get_l1_scaled_normal(self):
        engine = LOBEngine()
        book = engine.get_book("TST")
        book._rust_state = None
        book.apply_update(_np_bids(), _np_asks(), 5000)
        l1 = engine.get_l1_scaled("TST")
        assert l1 is not None
        assert l1[0] == 5000  # timestamp
        assert l1[1] == 1000000  # best_bid
        assert l1[2] == 1001000  # best_ask

    def test_get_l1_scaled_nonexistent(self):
        engine = LOBEngine()
        assert engine.get_l1_scaled("NONE") is None

    def test_get_l1_scaled_list_data(self):
        engine = LOBEngine()
        book = engine.get_book("TST")
        book._rust_state = None
        book.bids = _list_bids()
        book.asks = _list_asks()
        l1 = engine.get_l1_scaled("TST")
        assert l1 is not None
        assert l1[1] == 1000000

    def test_get_l1_scaled_empty_book(self):
        engine = LOBEngine()
        engine.get_book("TST")
        l1 = engine.get_l1_scaled("TST")
        assert l1 is not None
        assert l1[1] == 0
        assert l1[2] == 0


# ---------------------------------------------------------------------------
# LOBEngine: get_mid_price
# ---------------------------------------------------------------------------


class TestLOBEngineGetMidPrice:
    def test_get_mid_price_normal(self):
        engine = LOBEngine()
        book = engine.get_book("TST")
        book._rust_state = None
        book.apply_update(_np_bids(), _np_asks(), 1000)
        mid = engine.get_mid_price("TST")
        assert mid == (1000000 + 1001000) // 2

    def test_get_mid_price_nonexistent(self):
        engine = LOBEngine()
        assert engine.get_mid_price("NONE") is None

    def test_get_mid_price_empty_book(self):
        engine = LOBEngine()
        engine.get_book("TST")
        assert engine.get_mid_price("TST") is None


# ---------------------------------------------------------------------------
# LOBEngine: evict_stale_symbols
# ---------------------------------------------------------------------------


class TestLOBEngineEviction:
    def test_evict_stale_symbols(self):
        engine = LOBEngine()
        engine._eviction_ttl_ns = 1  # Very short TTL
        engine._eviction_last_run_ns = 0
        book = engine.get_book("OLD")
        book.exch_ts = 1  # Very old timestamp
        count = engine.evict_stale_symbols()
        assert count == 1
        assert "OLD" not in engine.books

    def test_evict_stale_symbols_rate_limited(self):
        engine = LOBEngine()
        engine._eviction_ttl_ns = 1
        engine._eviction_last_run_ns = 99999999999999999999  # Far future
        count = engine.evict_stale_symbols()
        assert count == 0

    def test_evict_stale_symbols_disabled(self):
        engine = LOBEngine()
        engine._eviction_ttl_ns = 0
        count = engine.evict_stale_symbols()
        assert count == 0

    def test_evict_clears_cached_symbol(self):
        engine = LOBEngine()
        engine._eviction_ttl_ns = 1
        engine._eviction_last_run_ns = 0
        engine.get_book("OLD")
        engine.books["OLD"].exch_ts = 1
        engine._last_symbol = "OLD"
        engine.evict_stale_symbols()
        assert engine._last_symbol is None


# ---------------------------------------------------------------------------
# LOBEngine: reset_books and reset_books_for_symbols
# ---------------------------------------------------------------------------


class TestLOBEngineReset:
    def test_reset_books(self):
        engine = LOBEngine()
        engine.get_book("A")
        engine.get_book("B")
        engine.reset_books()
        assert len(engine.books) == 0
        assert engine._last_symbol is None

    def test_reset_books_for_symbols(self):
        engine = LOBEngine()
        engine.get_book("A")
        engine.get_book("B")
        engine.get_book("C")
        engine._last_symbol = "B"
        engine.reset_books_for_symbols({"A", "B"})
        assert "A" not in engine.books
        assert "B" not in engine.books
        assert "C" in engine.books
        assert engine._last_symbol is None

    def test_reset_books_for_symbols_not_cached(self):
        engine = LOBEngine()
        engine.get_book("A")
        engine.get_book("B")
        engine._last_symbol = "B"
        engine.reset_books_for_symbols({"A"})
        assert engine._last_symbol == "B"  # Not cleared


# ---------------------------------------------------------------------------
# LOBEngine: _NoopLock
# ---------------------------------------------------------------------------


def test_noop_lock():
    lock = _NoopLock()
    with lock:
        pass
    assert lock.__exit__(None, None, None) is False


# ---------------------------------------------------------------------------
# LOBEngine: stop
# ---------------------------------------------------------------------------


def test_lob_engine_stop():
    engine = LOBEngine()
    engine.stop()  # No-op when no task
    engine._metrics_task = MagicMock()
    engine.stop()
    engine._metrics_task.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# LOBEngine: _record_lob_metrics cardinality guard
# ---------------------------------------------------------------------------


class TestLOBEngineMetrics:
    def test_metrics_cardinality_guard(self):
        engine = LOBEngine()
        engine._metrics_enabled = True
        engine._metrics_max_label_symbols = 2
        engine._metrics_known_symbols = {"A", "B"}
        engine._record_lob_metrics("C", False)
        # "C" should be mapped to "_other"
        assert ("_other", "BidAsk") in engine._metrics_pending_updates

    def test_flush_metrics_disabled(self):
        engine = LOBEngine()
        engine._metrics_enabled = False
        engine.metrics = None
        engine._flush_metrics()  # Should not raise
