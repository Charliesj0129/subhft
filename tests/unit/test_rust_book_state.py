"""Parity tests: RustBookState vs Python BookState.

Validates that the Rust-accelerated book state produces identical results
to the pure-Python fallback for all hot-path operations.
"""

import os

import numpy as np
import pytest

# Ensure Rust acceleration is available for testing.
try:
    try:
        from hft_platform import rust_core

        RustBookState = rust_core.RustBookState
    except Exception:
        import rust_core

        RustBookState = rust_core.RustBookState
except Exception:
    RustBookState = None


@pytest.fixture
def sample_bids():
    return np.array([[100_0000, 50], [99_0000, 30], [98_0000, 20]], dtype=np.int64)


@pytest.fixture
def sample_asks():
    return np.array([[101_0000, 40], [102_0000, 25], [103_0000, 15]], dtype=np.int64)


@pytest.fixture
def empty_bids():
    return np.empty((0, 2), dtype=np.int64)


@pytest.fixture
def empty_asks():
    return np.empty((0, 2), dtype=np.int64)


@pytest.mark.skipif(RustBookState is None, reason="Rust extension not available")
class TestRustBookState:
    def test_basic_apply_update(self, sample_bids, sample_asks):
        bs = RustBookState("TEST")
        result = bs.apply_update(sample_bids, sample_asks, 1000)
        assert result is True
        assert bs.exch_ts == 1000
        assert bs.version == 1
        # best_bid = 100_0000, best_ask = 101_0000
        assert bs.mid_price_x2 == 100_0000 + 101_0000
        assert bs.spread == 101_0000 - 100_0000
        assert bs.bid_depth_total == 50 + 30 + 20  # 100
        assert bs.ask_depth_total == 40 + 25 + 15  # 80

    def test_imbalance_calculation(self, sample_bids, sample_asks):
        bs = RustBookState("TEST")
        bs.apply_update(sample_bids, sample_asks, 1000)
        # imbalance = (bid_vol_top - ask_vol_top) / (bid_vol_top + ask_vol_top)
        # = (50 - 40) / (50 + 40) = 10/90 ≈ 0.1111
        assert abs(bs.imbalance - (50 - 40) / (50 + 40)) < 1e-10

    def test_late_packet_rejected(self, sample_bids, sample_asks):
        bs = RustBookState("TEST")
        bs.apply_update(sample_bids, sample_asks, 2000)
        result = bs.apply_update(sample_bids, sample_asks, 1000)
        assert result is False
        assert bs.exch_ts == 2000

    def test_get_stats_tuple(self, sample_bids, sample_asks):
        bs = RustBookState("TEST")
        bs.apply_update(sample_bids, sample_asks, 1000)
        stats = bs.get_stats_tuple()
        assert isinstance(stats, tuple)
        assert len(stats) == 9
        symbol, ts, mid_x2, spread, imb, best_bid, best_ask, bid_depth, ask_depth = stats
        assert symbol == "TEST"
        assert ts == 1000
        assert mid_x2 == 100_0000 + 101_0000
        assert spread == 101_0000 - 100_0000
        assert best_bid == 100_0000
        assert best_ask == 101_0000
        assert bid_depth == 100
        assert ask_depth == 80

    def test_get_l1_scaled(self, sample_bids, sample_asks):
        bs = RustBookState("TEST")
        bs.apply_update(sample_bids, sample_asks, 1000)
        l1 = bs.get_l1_scaled()
        assert isinstance(l1, tuple)
        assert len(l1) == 7
        ts, best_bid, best_ask, mid_x2, spread, bid_depth, ask_depth = l1
        assert ts == 1000
        assert best_bid == 100_0000
        assert best_ask == 101_0000

    def test_empty_book(self, empty_bids, empty_asks):
        bs = RustBookState("EMPTY")
        bs.apply_update(empty_bids, empty_asks, 500)
        assert bs.mid_price_x2 == 0
        assert bs.spread == 0
        assert bs.imbalance == 0.0
        assert bs.bid_depth_total == 0
        assert bs.ask_depth_total == 0
        stats = bs.get_stats_tuple()
        assert stats[5] == 0  # best_bid
        assert stats[6] == 0  # best_ask

    def test_one_sided_bids_only(self, sample_bids, empty_asks):
        bs = RustBookState("ONE_SIDE")
        bs.apply_update(sample_bids, empty_asks, 600)
        assert bs.mid_price_x2 == 0
        assert bs.spread == 0
        assert bs.bid_depth_total == 100
        assert bs.ask_depth_total == 0

    def test_one_sided_asks_only(self, empty_bids, sample_asks):
        bs = RustBookState("ONE_SIDE")
        bs.apply_update(empty_bids, sample_asks, 700)
        assert bs.mid_price_x2 == 0
        assert bs.ask_depth_total == 80

    def test_update_tick(self):
        bs = RustBookState("TICK")
        result = bs.update_tick(150_0000, 100, 900)
        assert result is True
        assert bs.last_price == 150_0000
        assert bs.last_volume == 100
        assert bs.exch_ts == 900

    def test_update_tick_late_rejected(self):
        bs = RustBookState("TICK")
        bs.update_tick(150_0000, 100, 1000)
        result = bs.update_tick(150_0000, 200, 500)
        assert result is False
        assert bs.last_volume == 100  # unchanged

    def test_apply_update_with_stats(self, sample_bids, sample_asks):
        bs = RustBookState("STATS")
        result = bs.apply_update_with_stats(
            sample_bids,
            sample_asks,
            1000,
            best_bid=100_0000,
            best_ask=101_0000,
            bid_depth=100,
            ask_depth=80,
            imbalance=0.111,
        )
        assert result is True
        assert bs.mid_price_x2 == 100_0000 + 101_0000
        assert bs.spread == 101_0000 - 100_0000
        assert abs(bs.imbalance - 0.111) < 1e-10
        assert bs.bid_depth_total == 100
        assert bs.ask_depth_total == 80

    def test_sequential_updates(self, sample_bids, sample_asks):
        bs = RustBookState("SEQ")
        bs.apply_update(sample_bids, sample_asks, 1000)
        assert bs.version == 1

        # Update with new data
        new_bids = np.array([[105_0000, 60]], dtype=np.int64)
        new_asks = np.array([[106_0000, 45]], dtype=np.int64)
        bs.apply_update(new_bids, new_asks, 2000)
        assert bs.version == 2
        assert bs.mid_price_x2 == 105_0000 + 106_0000
        assert bs.bid_depth_total == 60
        assert bs.ask_depth_total == 45


@pytest.mark.skipif(RustBookState is None, reason="Rust extension not available")
class TestRustBookStateParity:
    """Verify RustBookState produces identical results to Python BookState."""

    def test_parity_with_python_bookstate(self, sample_bids, sample_asks):
        """Both paths should produce identical stats for the same input."""
        # Force Python path
        orig_env = os.environ.get("HFT_LOB_RUST_BOOKSTATE")
        os.environ["HFT_LOB_RUST_BOOKSTATE"] = "0"
        try:
            from importlib import reload

            import hft_platform.feed_adapter.lob_engine as lob_mod

            reload(lob_mod)
            py_book = lob_mod.BookState("TEST_PARITY")
            py_book.apply_update(sample_bids.copy(), sample_asks.copy(), 1000)
            py_stats = py_book.get_stats_tuple()
        finally:
            if orig_env is not None:
                os.environ["HFT_LOB_RUST_BOOKSTATE"] = orig_env
            else:
                os.environ.pop("HFT_LOB_RUST_BOOKSTATE", None)

        # Rust path
        rust_book = RustBookState("TEST_PARITY")
        rust_book.apply_update(sample_bids, sample_asks, 1000)
        rust_stats = rust_book.get_stats_tuple()

        # Compare all 9 fields
        assert py_stats[0] == rust_stats[0]  # symbol
        assert py_stats[1] == rust_stats[1]  # exch_ts
        assert py_stats[2] == rust_stats[2]  # mid_price_x2
        assert py_stats[3] == rust_stats[3]  # spread
        assert abs(py_stats[4] - rust_stats[4]) < 1e-10  # imbalance
        assert py_stats[5] == rust_stats[5]  # best_bid
        assert py_stats[6] == rust_stats[6]  # best_ask
        assert py_stats[7] == rust_stats[7]  # bid_depth
        assert py_stats[8] == rust_stats[8]  # ask_depth
