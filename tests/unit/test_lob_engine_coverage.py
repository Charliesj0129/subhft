"""
Targeted coverage tests for feed_adapter/lob_engine.py.

Focuses on branches not covered by existing tests:
- NoopLock behaviour
- BookState with Rust disabled (pure-Python _recompute)
- apply_update_with_stats / apply_update_with_stats_fields
- get_stats / get_stats_tuple
- get_l1_scaled
- LOBEngine.process_event tuple fast-paths
- LOBEngine.get_book_snapshot
- LOBEngine metrics paths
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import hft_platform.feed_adapter.lob_engine as lob_mod
from hft_platform.events import BidAskEvent, FusedBookStats, LOBStatsEvent, MetaData, TickEvent
from hft_platform.feed_adapter.lob_engine import BookState, LOBEngine, _NoopLock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bids_np():
    return np.array([[1000000, 10], [999000, 5]], dtype=np.int64)


def _make_asks_np():
    return np.array([[1001000, 8], [1002000, 3]], dtype=np.int64)


def _make_bid_ask_event(symbol="2330", ts=1_000_000_000):
    meta = MetaData(seq=1, topic="bidask", source_ts=ts, local_ts=ts)
    return BidAskEvent(
        meta=meta,
        symbol=symbol,
        bids=_make_bids_np(),
        asks=_make_asks_np(),
    )


def _make_tick_event(symbol="2330", price=1_000_000, volume=5, ts=1_000_000_000):
    meta = MetaData(seq=2, topic="tick", source_ts=ts, local_ts=ts)
    return TickEvent(
        meta=meta,
        symbol=symbol,
        price=price,
        volume=volume,
        total_volume=100,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )


# ---------------------------------------------------------------------------
# _NoopLock
# ---------------------------------------------------------------------------

class TestNoopLock:
    def test_noop_lock_exit_returns_false(self):
        lock = _NoopLock()
        result = lock.__exit__(None, None, None)
        assert result is False

    def test_noop_lock_context_manager(self):
        lock = _NoopLock()
        entered = False
        with lock:
            entered = True
        assert entered


# ---------------------------------------------------------------------------
# BookState — pure Python _recompute (Rust disabled)
# ---------------------------------------------------------------------------

class TestBookStatePureRecompute:
    @pytest.fixture
    def book_no_rust(self, monkeypatch):
        """BookState with Rust and RustBookState disabled."""
        monkeypatch.setattr(lob_mod, "_RUST_ENABLED", False)
        monkeypatch.setattr(lob_mod, "_RUST_COMPUTE_STATS", None)
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        return BookState("2330")

    def test_recompute_with_numpy_arrays(self, book_no_rust):
        bids = np.array([[1000000, 10], [999000, 5]], dtype=np.int64)
        asks = np.array([[1001000, 8], [1002000, 3]], dtype=np.int64)
        book_no_rust.apply_update(bids, asks, 1_000_000_000)
        assert book_no_rust.mid_price_x2 == 1000000 + 1001000
        assert book_no_rust.spread == 1001000 - 1000000
        assert book_no_rust.bid_depth_total == 15
        assert book_no_rust.ask_depth_total == 11

    def test_recompute_with_list_bids_asks(self, book_no_rust):
        bids = [[1000000, 10], [999000, 5]]
        asks = [[1001000, 8], [1002000, 3]]
        book_no_rust.apply_update(bids, asks, 1_000_000_000)
        assert book_no_rust.bid_depth_total == 15
        assert book_no_rust.ask_depth_total == 11
        assert book_no_rust.mid_price_x2 == 2001000

    def test_recompute_one_sided_bids_only(self, book_no_rust):
        """When asks are empty, mid_price_x2 and spread should be 0."""
        bids = [[1000000, 10]]
        book_no_rust.apply_update(bids, [], 1_000_000_000)
        assert book_no_rust.mid_price_x2 == 0
        assert book_no_rust.spread == 0
        assert book_no_rust.imbalance == 0.0

    def test_recompute_empty_numpy_arrays(self, book_no_rust):
        bids = np.empty((0, 2), dtype=np.int64)
        asks = np.empty((0, 2), dtype=np.int64)
        book_no_rust.apply_update(bids, asks, 1_000_000_000)
        assert book_no_rust.bid_depth_total == 0
        assert book_no_rust.ask_depth_total == 0
        assert book_no_rust.mid_price_x2 == 0

    def test_late_packet_rejected(self, book_no_rust):
        bids = [[1000000, 10]]
        asks = [[1001000, 8]]
        book_no_rust.apply_update(bids, asks, 2_000_000_000)
        version_before = book_no_rust.version
        # Send older timestamp — should be ignored
        book_no_rust.apply_update([[999000, 5]], asks, 1_000_000_000)
        assert book_no_rust.version == version_before

    def test_update_tick_late_packet_rejected(self, book_no_rust):
        book_no_rust.update_tick(1_000_000, 5, 2_000_000_000)
        old_price = book_no_rust.last_price
        book_no_rust.update_tick(900_000, 3, 1_000_000_000)
        assert book_no_rust.last_price == old_price


# ---------------------------------------------------------------------------
# BookState — get_stats, get_stats_tuple
# ---------------------------------------------------------------------------

class TestBookStateStats:
    @pytest.fixture
    def book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        b = BookState("2330")
        bids = np.array([[1000000, 10], [999000, 5]], dtype=np.int64)
        asks = np.array([[1001000, 8], [1002000, 3]], dtype=np.int64)
        b.apply_update(bids, asks, 1_000_000_000)
        return b

    def test_get_stats_returns_lobstatsevent(self, book):
        stats = book.get_stats()
        assert isinstance(stats, LOBStatsEvent)
        assert stats.symbol == "2330"
        assert stats.best_bid == 1000000
        assert stats.best_ask == 1001000

    def test_get_stats_tuple_returns_tuple(self, book):
        t = book.get_stats_tuple()
        assert isinstance(t, tuple)
        assert len(t) == 10
        assert t[0] == "lobstats"
        assert t[1] == "2330"
        assert t[6] == 1000000  # best_bid
        assert t[7] == 1001000  # best_ask

    def test_get_stats_empty_book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        b = BookState("EMPTY")
        stats = b.get_stats()
        assert stats.best_bid == 0
        assert stats.best_ask == 0

    def test_get_stats_tuple_list_bids(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        monkeypatch.setattr(lob_mod, "_RUST_COMPUTE_STATS", None)
        monkeypatch.setattr(lob_mod, "_RUST_ENABLED", False)
        b = BookState("LIST")
        b.apply_update([[1000000, 10]], [[1001000, 8]], 1_000_000_000)
        t = b.get_stats_tuple()
        assert t[0] == "lobstats"
        assert t[6] == 1000000
        assert t[7] == 1001000


# ---------------------------------------------------------------------------
# BookState — apply_update_with_stats_fields
# ---------------------------------------------------------------------------

class TestApplyUpdateWithStats:
    def test_apply_update_with_stats(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        b = BookState("2330")
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        stats = (1000000, 1001000, 10, 8, 1000500.0, 1000.0, 0.111)
        b.apply_update_with_stats(bids, asks, 1_000_000_000, stats)
        assert b.mid_price_x2 == 2001000
        assert b.spread == 1000
        assert b.bid_depth_total == 10
        assert b.ask_depth_total == 8

    def test_apply_update_with_stats_fields_late_packet(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        b = BookState("2330")
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        b.apply_update_with_stats_fields(bids, asks, 2_000_000_000, 1000000, 1001000, 10, 8, 0.0, 0.0, 0.1)
        v = b.version
        b.apply_update_with_stats_fields(bids, asks, 1_000_000_000, 1000000, 1001000, 10, 8, 0.0, 0.0, 0.1)
        assert b.version == v  # Late packet ignored

    def test_apply_update_with_stats_fields_empty_bids(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        b = BookState("2330")
        bids = np.empty((0, 2), dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        b.apply_update_with_stats_fields(bids, asks, 1_000_000_000, 0, 1001000, 0, 8, 0.0, 0.0, 0.0)
        assert b.bids == []

    def test_apply_update_with_stats_fields_list_conversion(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        b = BookState("2330")
        bids = [[1000000, 10]]
        asks = [[1001000, 8]]
        b.apply_update_with_stats_fields(bids, asks, 1_000_000_000, 1000000, 1001000, 10, 8, 0.0, 0.0, 0.1)
        assert b.mid_price_x2 == 2001000


# ---------------------------------------------------------------------------
# LOBEngine — tuple fast-paths in process_event
# ---------------------------------------------------------------------------

class TestLOBEngineTupleFastPath:
    def test_process_bidask_tuple_6_fields(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_STATS_NONE", False)
        monkeypatch.setattr(lob_mod, "_STATS_TUPLE", False)
        engine = LOBEngine()
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        event_tuple = ("bidask", "2330", bids, asks, 1_000_000_000, True)
        result = engine.process_event(event_tuple)
        assert isinstance(result, LOBStatsEvent)

    def test_process_bidask_tuple_13_fields(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_STATS_NONE", False)
        monkeypatch.setattr(lob_mod, "_STATS_TUPLE", False)
        engine = LOBEngine()
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        event_tuple = (
            "bidask", "2330", bids, asks, 1_000_000_000, False,
            1000000, 1001000, 10, 8, 1000500.0, 1000.0, 0.111,
        )
        result = engine.process_event(event_tuple)
        assert isinstance(result, LOBStatsEvent)

    def test_process_tick_tuple(self, monkeypatch):
        engine = LOBEngine()
        event_tuple = ("tick", "2330", 1_000_000, 5, 100, False, False, 1_000_000_000)
        result = engine.process_event(event_tuple)
        assert result is None
        book = engine.get_book("2330")
        assert book.last_price == 1_000_000

    def test_process_stats_none_mode(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_STATS_NONE", True)
        engine = LOBEngine()
        event = _make_bid_ask_event()
        result = engine.process_event(event)
        assert result is None

    def test_process_stats_tuple_mode(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_STATS_NONE", False)
        monkeypatch.setattr(lob_mod, "_STATS_TUPLE", True)
        engine = LOBEngine()
        event = _make_bid_ask_event()
        result = engine.process_event(event)
        assert isinstance(result, tuple)

    def test_process_unknown_tuple_returns_none(self):
        engine = LOBEngine()
        result = engine.process_event(("unknown", "2330"))
        assert result is None

    def test_process_empty_tuple_returns_none(self):
        engine = LOBEngine()
        result = engine.process_event(())
        assert result is None

    def test_process_none_event_returns_none(self):
        engine = LOBEngine()
        result = engine.process_event(None)
        assert result is None


# ---------------------------------------------------------------------------
# LOBEngine — BidAskEvent with stats and fused bypass
# ---------------------------------------------------------------------------

class TestLOBEngineEventDispatch:
    def test_process_bidask_event_with_stats(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_FUSED_BYPASS", False)
        monkeypatch.setattr(lob_mod, "_STATS_NONE", False)
        monkeypatch.setattr(lob_mod, "_STATS_TUPLE", False)
        engine = LOBEngine()
        meta = MetaData(seq=1, topic="bidask", source_ts=1_000_000_000, local_ts=1_000_000_000)
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        event = BidAskEvent(
            meta=meta,
            symbol="2330",
            bids=bids,
            asks=asks,
            stats=(1000000, 1001000, 10, 8, 1000500.0, 1000.0, 0.111),
        )
        result = engine.process_event(event)
        assert result is not None

    def test_process_bidaskevent_no_stats(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_FUSED_BYPASS", False)
        monkeypatch.setattr(lob_mod, "_STATS_NONE", False)
        monkeypatch.setattr(lob_mod, "_STATS_TUPLE", False)
        engine = LOBEngine()
        result = engine.process_event(_make_bid_ask_event())
        assert isinstance(result, LOBStatsEvent)

    def test_process_tick_event(self, monkeypatch):
        engine = LOBEngine()
        result = engine.process_event(_make_tick_event())
        assert result is None

    def test_process_fused_bypass_event(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_FUSED_BYPASS", True)
        monkeypatch.setattr(lob_mod, "_STATS_NONE", False)
        monkeypatch.setattr(lob_mod, "_STATS_TUPLE", False)
        engine = LOBEngine()
        meta = MetaData(seq=1, topic="bidask", source_ts=1_000_000_000, local_ts=1_000_000_000)
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        # fused_stats: (best_bid, best_ask, bid_depth, ask_depth, mid_x2, spread_scaled, imbalance)
        event = BidAskEvent(
            meta=meta,
            symbol="2330",
            bids=bids,
            asks=asks,
            fused_stats=FusedBookStats(1000000, 1001000, 10, 8, 2001000, 1000, 0.111),
        )
        result = engine.process_event(event)
        book = engine.get_book("2330")
        assert book.mid_price_x2 == 2001000

    def test_process_fused_bypass_late_packet(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_FUSED_BYPASS", True)
        engine = LOBEngine()
        # First update at ts=2000
        meta1 = MetaData(seq=1, topic="bidask", source_ts=2_000_000_000, local_ts=2_000_000_000)
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1001000, 8]], dtype=np.int64)
        fused = FusedBookStats(1000000, 1001000, 10, 8, 2001000, 1000, 0.1)
        engine.process_event(BidAskEvent(meta=meta1, symbol="2330", bids=bids, asks=asks, fused_stats=fused))
        book = engine.get_book("2330")
        v_before = book.version
        # Late update at ts=1000
        meta2 = MetaData(seq=2, topic="bidask", source_ts=1_000_000_000, local_ts=1_000_000_000)
        engine.process_event(BidAskEvent(meta=meta2, symbol="2330", bids=bids, asks=asks, fused_stats=fused))
        assert book.version == v_before  # Late packet ignored


# ---------------------------------------------------------------------------
# LOBEngine — get_book_snapshot
# ---------------------------------------------------------------------------

class TestGetBookSnapshot:
    def test_missing_symbol_returns_none(self):
        engine = LOBEngine()
        assert engine.get_book_snapshot("NONEXISTENT") is None

    def test_snapshot_numpy_book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_STATS_NONE", False)
        engine = LOBEngine()
        engine.process_event(_make_bid_ask_event("2330"))
        snap = engine.get_book_snapshot("2330")
        assert snap is not None
        assert snap["symbol"] == "2330"
        assert snap["best_bid"] > 0
        assert snap["best_ask"] > snap["best_bid"]
        assert "mid_price" in snap
        assert "spread" in snap

    def test_snapshot_empty_book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        engine = LOBEngine()
        _ = engine.get_book("2330")  # Create empty book
        snap = engine.get_book_snapshot("2330")
        assert snap is not None
        assert snap["best_bid"] == 0
        assert snap["best_ask"] == 0

    def test_snapshot_list_based_book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RUST_ENABLED", False)
        monkeypatch.setattr(lob_mod, "_FORCE_NUMPY", False)
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        engine = LOBEngine()
        bids = [[1000000, 10]]
        asks = [[1001000, 8]]
        book = engine.get_book("2330")
        book.apply_update(bids, asks, 1_000_000_000)
        snap = engine.get_book_snapshot("2330")
        assert snap["best_bid"] == 1000000
        assert snap["best_ask"] == 1001000


# ---------------------------------------------------------------------------
# LOBEngine — get_l1_scaled
# ---------------------------------------------------------------------------

class TestGetL1Scaled:
    def test_missing_symbol_returns_none(self):
        engine = LOBEngine()
        assert engine.get_l1_scaled("NONEXISTENT") is None

    def test_l1_scaled_numpy_book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        engine = LOBEngine()
        engine.process_event(_make_bid_ask_event("2330"))
        l1 = engine.get_l1_scaled("2330")
        assert l1 is not None
        assert len(l1) == 7
        ts, bb, ba, mpx2, spread, bd, ad = l1
        assert bb == 1000000
        assert ba == 1001000
        assert mpx2 == bb + ba
        assert spread == ba - bb

    def test_l1_scaled_empty_book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        engine = LOBEngine()
        _ = engine.get_book("2330")
        l1 = engine.get_l1_scaled("2330")
        assert l1 is not None
        _, bb, ba, mpx2, spread, bd, ad = l1
        assert bb == 0
        assert ba == 0

    def test_l1_scaled_list_based_book(self, monkeypatch):
        monkeypatch.setattr(lob_mod, "_RUST_ENABLED", False)
        monkeypatch.setattr(lob_mod, "_FORCE_NUMPY", False)
        monkeypatch.setattr(lob_mod, "_RustBookState", None)
        monkeypatch.setattr(lob_mod, "_RUST_BOOK_STATE_ENABLED", False)
        engine = LOBEngine()
        book = engine.get_book("2330")
        book.apply_update([[1000000, 10]], [[1001000, 8]], 1_000_000_000)
        l1 = engine.get_l1_scaled("2330")
        assert l1[1] == 1000000
        assert l1[2] == 1001000


# ---------------------------------------------------------------------------
# LOBEngine — metrics
# ---------------------------------------------------------------------------

class TestLOBEngineMetrics:
    def test_record_lob_metrics_no_metrics_enabled(self):
        """When metrics are disabled, _record_lob_metrics is a no-op."""
        engine = LOBEngine()
        engine._metrics_enabled = False
        engine.metrics = None
        engine._record_lob_metrics("2330", True)
        assert engine._metrics_pending_total == 0

    def test_flush_metrics_no_metrics_enabled(self):
        engine = LOBEngine()
        engine._metrics_enabled = False
        engine.metrics = None
        engine._flush_metrics()  # Should not raise when metrics disabled
        assert engine._metrics_enabled is False

    def test_start_metrics_worker_only_once(self):
        engine = LOBEngine()
        loop = asyncio.new_event_loop()
        try:
            engine.start_metrics_worker(loop, interval_ms=5)
            task1 = engine._metrics_task
            engine.start_metrics_worker(loop, interval_ms=5)
            # Second call is a no-op
            assert engine._metrics_task is task1
        finally:
            if engine._metrics_task:
                engine._metrics_task.cancel()
            loop.close()

    def test_get_book_caches_last_symbol(self):
        engine = LOBEngine()
        b1 = engine.get_book("2330")
        b2 = engine.get_book("2330")
        assert b1 is b2
        assert engine._last_symbol == "2330"
        assert engine._last_book is b1

    def test_get_book_different_symbol_clears_cache(self):
        engine = LOBEngine()
        b_a = engine.get_book("AAAA")
        b_b = engine.get_book("BBBB")
        assert engine._last_symbol == "BBBB"
        assert engine._last_book is b_b
        assert b_a is not b_b

    def test_flush_metrics_applies_cap_symbol_guard(self):
        """cap_symbol() must be applied at label-emission time to prevent cardinality explosion."""
        engine = LOBEngine()
        mock_metrics = MagicMock()
        # cap_symbol returns "_other" for symbols beyond the cap
        mock_metrics.cap_symbol.return_value = "_other"
        engine.metrics = mock_metrics
        engine._metrics_enabled = True

        # Inject a pending update and snapshot
        engine._metrics_pending_updates[("RARE_SYM", "bid")] = 3
        engine._metrics_pending_snapshots["RARE_SYM"] = 1

        engine._flush_metrics()

        # cap_symbol must have been called for both emission points
        assert mock_metrics.cap_symbol.call_count == 2
        # The label passed to Prometheus must be the capped value, not the raw symbol
        mock_metrics.lob_updates_total.labels.assert_called_once_with(
            symbol="_other", type="bid"
        )
        mock_metrics.lob_snapshots_total.labels.assert_called_once_with(symbol="_other")
