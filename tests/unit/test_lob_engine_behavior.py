"""Coverage tests for feed_adapter/lob_engine.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from hft_platform.feed_adapter.lob_engine import BookState, _NoopLock

# ── _NoopLock ─────────────────────────────────────────────────────────────


class TestNoopLock:
    def test_context_manager(self):
        lock = _NoopLock()
        with lock as val:
            assert val is lock

    def test_exit_returns_false(self):
        lock = _NoopLock()
        assert lock.__exit__(None, None, None) is False


# ── BookState construction ────────────────────────────────────────────────


class TestBookStateInit:
    def test_default(self):
        bs = BookState("2330")
        assert bs.symbol == "2330"
        assert bs.bids == []
        assert bs.asks == []
        assert bs.mid_price_x2 == 0

    def test_lock_is_real_lock_by_default(self):
        """H11: HFT_LOB_LOCKS default must be enabled so apply_update
        is not racy against concurrent readers under HFT_LOB_READ_LOCKS=1.
        Torn reads occurred silently because the NoopLock context was
        still acquired on the read side but protected nothing."""
        from threading import Lock as _RealLock

        import hft_platform.feed_adapter.lob_engine as mod

        assert mod._LOCKS_ENABLED is True
        bs = BookState("2330")
        # The real threading.Lock is not a class, so check it quacks like one:
        assert hasattr(bs.lock, "acquire")
        assert hasattr(bs.lock, "release")
        assert not isinstance(bs.lock, mod._NoopLock)
        # Sanity check: the type matches the module-level Lock factory.
        assert type(bs.lock).__module__ == _RealLock.__module__

    def test_rust_state_disabled(self):
        import hft_platform.feed_adapter.lob_engine as mod

        old = mod._RUST_BOOK_STATE_ENABLED
        mod._RUST_BOOK_STATE_ENABLED = False
        try:
            bs = BookState("2330")
            assert bs._rust_state is None
        finally:
            mod._RUST_BOOK_STATE_ENABLED = old

    def test_rust_state_exception(self):
        import hft_platform.feed_adapter.lob_engine as mod

        old_cls = mod._RustBookState
        old_en = mod._RUST_BOOK_STATE_ENABLED
        mod._RUST_BOOK_STATE_ENABLED = True
        mod._RustBookState = MagicMock(side_effect=RuntimeError("no rust"))
        try:
            bs = BookState("2330")
            assert bs._rust_state is None
        finally:
            mod._RustBookState = old_cls
            mod._RUST_BOOK_STATE_ENABLED = old_en


# ── apply_update branches ─────────────────────────────────────────────────


class TestApplyUpdate:
    def test_late_packet_skipped(self):
        bs = BookState("2330")
        bs.exch_ts = 100
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1010000, 10]], dtype=np.int64)
        bs.apply_update(bids, asks, 50)
        assert bs.bids == []  # not updated

    def test_numpy_int64(self):
        bs = BookState("2330")
        bs._rust_state = None
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1010000, 5]], dtype=np.int64)
        bs.apply_update(bids, asks, 100)
        assert bs.exch_ts == 100
        assert bs.mid_price_x2 == 2010000
        assert bs.spread == 10000

    def test_numpy_int32_coerced(self):
        bs = BookState("2330")
        bs._rust_state = None
        bids = np.array([[1000000, 10]], dtype=np.int32)
        asks = np.array([[1010000, 5]], dtype=np.int32)
        bs.apply_update(bids, asks, 100)
        assert bs.exch_ts == 100

    def test_list_input(self):
        import hft_platform.feed_adapter.lob_engine as mod

        old_fn = mod._FORCE_NUMPY
        mod._FORCE_NUMPY = False
        try:
            bs = BookState("2330")
            bs._rust_state = None
            bs.apply_update([[1000000, 10]], [[1010000, 5]], 100)
            assert bs.bids == [[1000000, 10]]
        finally:
            mod._FORCE_NUMPY = old_fn

    def test_empty_bids_asks(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.apply_update([], [], 100)
        assert bs.bids == []
        assert bs.asks == []
        assert bs.mid_price_x2 == 0

    def test_rust_book_state_fast_path(self):
        bs = BookState("2330")
        mock_rs = MagicMock()
        mock_rs.mid_price_x2 = 2010000
        mock_rs.spread = 10000
        mock_rs.imbalance = 0.5
        mock_rs.bid_depth_total = 100
        mock_rs.ask_depth_total = 50
        bs._rust_state = mock_rs
        bids = np.array([[1000000, 100]], dtype=np.int64)
        asks = np.array([[1010000, 50]], dtype=np.int64)
        bs.apply_update(bids, asks, 200)
        mock_rs.apply_update.assert_called_once()
        assert bs.mid_price_x2 == 2010000

    def test_rust_book_state_exception_fallback(self):
        bs = BookState("2330")
        mock_rs = MagicMock()
        mock_rs.apply_update.side_effect = RuntimeError("rust error")
        bs._rust_state = mock_rs
        bids = np.array([[1000000, 10]], dtype=np.int64)
        asks = np.array([[1010000, 5]], dtype=np.int64)
        bs.apply_update(bids, asks, 200)
        # Falls back to Python _recompute
        assert bs.mid_price_x2 == 2010000

    def test_stats_none_mode(self):
        import hft_platform.feed_adapter.lob_engine as mod

        old = mod._STATS_NONE
        mod._STATS_NONE = True
        try:
            bs = BookState("2330")
            bs._rust_state = None
            bids = np.array([[1000000, 10]], dtype=np.int64)
            asks = np.array([[1010000, 5]], dtype=np.int64)
            bs.apply_update(bids, asks, 100)
            # Stats not recomputed in NONE mode
            assert bs.version == 1
        finally:
            mod._STATS_NONE = old

    def test_local_ts_enabled(self):
        import hft_platform.feed_adapter.lob_engine as mod

        old = mod._LOCAL_TS_ENABLED
        mod._LOCAL_TS_ENABLED = True
        try:
            bs = BookState("2330")
            bs._rust_state = None
            bs.apply_update([], [], 100)
            assert bs.local_ts > 0
        finally:
            mod._LOCAL_TS_ENABLED = old

    def test_get_stats_propagates_local_ts(self):
        import hft_platform.feed_adapter.lob_engine as mod

        old = mod._LOCAL_TS_ENABLED
        mod._LOCAL_TS_ENABLED = True
        try:
            bs = BookState("2330")
            bs._rust_state = None
            bids = np.array([[1000000, 10]], dtype=np.int64)
            asks = np.array([[1010000, 5]], dtype=np.int64)
            bs.apply_update(bids, asks, 100)
            stats = bs.get_stats()
            assert stats.local_ts == bs.local_ts
        finally:
            mod._LOCAL_TS_ENABLED = old

    def test_numpy_float64_coerced(self):
        """Float64 arrays should be coerced to int64."""
        bs = BookState("2330")
        bs._rust_state = None
        bids = np.array([[1000000.0, 10.0]], dtype=np.float64)
        asks = np.array([[1010000.0, 5.0]], dtype=np.float64)
        bs.apply_update(bids, asks, 100)
        assert bs.exch_ts == 100

    def test_empty_numpy_arrays(self):
        bs = BookState("2330")
        bs._rust_state = None
        bids = np.array([], dtype=np.int64).reshape(0, 2)
        asks = np.array([], dtype=np.int64).reshape(0, 2)
        bs.apply_update(bids, asks, 100)
        assert bs.bids == []
        assert bs.asks == []


# ── update_tick ───────────────────────────────────────────────────────────


class TestUpdateTick:
    def test_normal(self):
        bs = BookState("2330")
        bs.update_tick(5000000, 100, 200)
        assert bs.last_price == 5000000
        assert bs.last_volume == 100

    def test_late_packet(self):
        bs = BookState("2330")
        bs.exch_ts = 300
        bs.update_tick(5000000, 100, 200)
        assert bs.last_price == 0


# ── _recompute branches ──────────────────────────────────────────────────


class TestRecompute:
    def test_python_numpy(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = np.array([[1000000, 10], [990000, 20]], dtype=np.int64)
        bs.asks = np.array([[1010000, 5], [1020000, 15]], dtype=np.int64)
        bs._recompute()
        assert bs.mid_price_x2 == 2010000
        assert bs.spread == 10000
        assert bs.bid_depth_total == 30
        assert bs.ask_depth_total == 20
        assert abs(bs.imbalance - (10 - 5) / (10 + 5)) < 0.01

    def test_python_list(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = [[1000000, 10]]
        bs.asks = [[1010000, 5]]
        bs._recompute()
        assert bs.mid_price_x2 == 2010000
        assert bs.spread == 10000

    def test_empty_book(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = []
        bs.asks = []
        bs._recompute()
        assert bs.mid_price_x2 == 0
        assert bs.spread == 0

    def test_one_sided_bids_only(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = np.array([[1000000, 10]], dtype=np.int64)
        bs.asks = []
        bs._recompute()
        assert bs.mid_price_x2 == 0  # need both sides

    def test_one_sided_asks_only(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = []
        bs.asks = np.array([[1010000, 5]], dtype=np.int64)
        bs._recompute()
        assert bs.mid_price_x2 == 0

    def test_rust_compute_stats_exception_fallback(self):
        import hft_platform.feed_adapter.lob_engine as mod

        old = mod._RUST_COMPUTE_STATS
        mod._RUST_COMPUTE_STATS = MagicMock(side_effect=RuntimeError("rust err"))
        try:
            bs = BookState("2330")
            bs._rust_state = None
            bs.bids = np.array([[1000000, 10]], dtype=np.int64)
            bs.asks = np.array([[1010000, 5]], dtype=np.int64)
            bs._recompute()
            # Falls back to Python
            assert bs.mid_price_x2 == 2010000
        finally:
            mod._RUST_COMPUTE_STATS = old

    def test_zero_volume_imbalance(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = np.array([[1000000, 0]], dtype=np.int64)
        bs.asks = np.array([[1010000, 0]], dtype=np.int64)
        bs._recompute()
        assert bs.imbalance == 0.0

    def test_empty_numpy_bids(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = np.array([], dtype=np.int64).reshape(0, 2)
        bs.asks = np.array([[1010000, 5]], dtype=np.int64)
        bs._recompute()
        assert bs.bid_depth_total == 0


# ── get_stats ─────────────────────────────────────────────────────────────


class TestGetStats:
    def test_numpy(self):
        bs = BookState("2330")
        bs.bids = np.array([[1000000, 10]], dtype=np.int64)
        bs.asks = np.array([[1010000, 5]], dtype=np.int64)
        bs.mid_price_x2 = 2010000
        bs.spread = 10000
        bs.imbalance = 0.33
        stats = bs.get_stats()
        assert stats.symbol == "2330"
        assert stats.best_bid == 1000000
        assert stats.best_ask == 1010000

    def test_list(self):
        bs = BookState("2330")
        bs.bids = [[1000000, 10]]
        bs.asks = [[1010000, 5]]
        stats = bs.get_stats()
        assert stats.best_bid == 1000000

    def test_empty(self):
        bs = BookState("2330")
        stats = bs.get_stats()
        assert stats.best_bid == 0
        assert stats.best_ask == 0


# ── get_stats_tuple ───────────────────────────────────────────────────────


class TestGetStatsTuple:
    def test_with_rust_state(self):
        bs = BookState("2330")
        mock_rs = MagicMock()
        mock_rs.get_stats_tuple.return_value = (1, 2, 3, 4, 5, 6)
        bs._rust_state = mock_rs
        result = bs.get_stats_tuple()
        assert result == ("lobstats", 1, 2, 3, 4, 5, 6)

    def test_rust_exception_fallback(self):
        bs = BookState("2330")
        mock_rs = MagicMock()
        mock_rs.get_stats_tuple.side_effect = RuntimeError("err")
        bs._rust_state = mock_rs
        bs.bids = np.array([[1000000, 10]], dtype=np.int64)
        bs.asks = np.array([[1010000, 5]], dtype=np.int64)
        bs.mid_price_x2 = 2010000
        result = bs.get_stats_tuple()
        assert isinstance(result, tuple)

    def test_no_rust(self):
        bs = BookState("2330")
        bs._rust_state = None
        bs.bids = [[1000000, 10]]
        bs.asks = [[1010000, 5]]
        bs.mid_price_x2 = 2010000
        result = bs.get_stats_tuple()
        assert isinstance(result, tuple)

    def test_empty(self):
        bs = BookState("2330")
        bs._rust_state = None
        result = bs.get_stats_tuple()
        assert isinstance(result, tuple)


# ── LOBEngine.get_mid_price ─────────────────────────────────────────────


class TestLOBEngineGetMidPrice:
    def test_returns_mid_price_for_known_symbol(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = LOBEngine()
        bs = BookState("TXFD6")
        bs.mid_price_x2 = 400_0000  # bid 195 + ask 205 = 400 (x10000)
        engine.books["TXFD6"] = bs
        assert engine.get_mid_price("TXFD6") == 200_0000  # 200 x10000

    def test_returns_none_for_unknown_symbol(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = LOBEngine()
        assert engine.get_mid_price("UNKNOWN") is None

    def test_returns_none_when_mid_price_x2_is_zero(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = LOBEngine()
        bs = BookState("TXFD6")
        bs.mid_price_x2 = 0  # empty or one-sided book
        engine.books["TXFD6"] = bs
        assert engine.get_mid_price("TXFD6") is None
