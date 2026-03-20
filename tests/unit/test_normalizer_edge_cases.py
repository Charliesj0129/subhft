"""Edge-case tests for MarketDataNormalizer.

Covers mismatched array lengths, empty sides, zero prices, single-level books,
scratch-array path, snapshot fallback, and Python fallback when Rust is unavailable.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import yaml

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_normalizer(tmp_path, *, monkeypatch=None, symbols=None):
    """Build a MarketDataNormalizer with Rust disabled for deterministic Python path."""
    if monkeypatch is not None:
        monkeypatch.setenv("HFT_RUST_ACCEL", "0")
        monkeypatch.setenv("HFT_FUSED_NORMALIZER", "0")

    cfg_path = tmp_path / "symbols.yaml"
    if symbols is None:
        symbols = [
            {"code": "2330", "name": "TSMC", "exchange": "TSE", "price_scale": 10000, "tick_size": 0.01},
        ]
    cfg_path.write_text(yaml.dump({"symbols": symbols}))

    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        with (
            patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", False),
            patch("hft_platform.feed_adapter.normalizer._RUST_FORCE", False),
            patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", False),
            patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", False),
            patch("hft_platform.feed_adapter.normalizer._HAS_FUSED", False),
            patch("hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH", False),
        ):
            norm = MarketDataNormalizer(config_path=str(cfg_path))
            norm.metrics = None
            norm._fused = None
    return norm


# ===================================================================
# 1. Mismatched bid/ask array lengths
# ===================================================================


class TestMismatchedBidAskArrayLengths:
    """5 bids, 3 asks -- normalizer should produce valid output with correct counts."""

    def test_mismatched_bid_ask_array_lengths(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [100.0, 99.5, 99.0, 98.5, 98.0],
            "bid_volume": [10, 20, 30, 40, 50],
            "ask_price": [100.5, 101.0, 101.5],
            "ask_volume": [15, 25, 35],
        }
        result = norm.normalize_bidask(payload)

        assert isinstance(result, BidAskEvent)
        assert result.symbol == "2330"
        assert len(result.bids) == 5
        assert len(result.asks) == 3
        # Verify scaling on both sides
        assert result.bids[0][0] == 1_000_000  # 100.0 * 10000
        assert result.asks[0][0] == 1_005_000  # 100.5 * 10000


# ===================================================================
# 2. Empty bid array
# ===================================================================


class TestEmptyBidArray:
    """bids=empty, asks=normal -- should handle gracefully."""

    def test_empty_bid_array(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [],
            "bid_volume": [],
            "ask_price": [100.5, 101.0],
            "ask_volume": [15, 25],
        }
        result = norm.normalize_bidask(payload)

        assert isinstance(result, BidAskEvent)
        assert len(result.bids) == 0
        assert len(result.asks) == 2
        assert result.asks[0][0] == 1_005_000


# ===================================================================
# 3. Empty ask array
# ===================================================================


class TestEmptyAskArray:
    """bids=normal, asks=empty -- should handle gracefully."""

    def test_empty_ask_array(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [100.0, 99.5],
            "bid_volume": [10, 20],
            "ask_price": [],
            "ask_volume": [],
        }
        result = norm.normalize_bidask(payload)

        assert isinstance(result, BidAskEvent)
        assert len(result.bids) == 2
        assert len(result.asks) == 0
        assert result.bids[0][0] == 1_000_000


# ===================================================================
# 4. Zero price in bidask
# ===================================================================


class TestZeroPriceInBidAsk:
    """price=0 in bid/ask array -- Python fallback filters zero-price levels."""

    def test_zero_price_in_bidask(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [100.0, 0.0, 99.0],
            "bid_volume": [10, 20, 30],
            "ask_price": [0.0, 101.0],
            "ask_volume": [15, 25],
        }
        result = norm.normalize_bidask(payload)

        assert isinstance(result, BidAskEvent)
        # Python fallback list comp filters where price && volume are truthy
        # bid: 100.0 (kept), 0.0 (filtered), 99.0 (kept) => 2 levels
        assert len(result.bids) == 2
        assert result.bids[0][0] == 1_000_000
        assert result.bids[1][0] == 990_000
        # ask: 0.0 (filtered), 101.0 (kept) => 1 level
        assert len(result.asks) == 1
        assert result.asks[0][0] == 1_010_000


# ===================================================================
# 5. Single level book
# ===================================================================


class TestSingleLevelBook:
    """Only 1 bid, 1 ask level -- minimal valid book."""

    def test_single_level_book(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [5],
            "ask_price": [100.5],
            "ask_volume": [3],
        }
        result = norm.normalize_bidask(payload)

        assert isinstance(result, BidAskEvent)
        assert len(result.bids) == 1
        assert len(result.asks) == 1
        assert result.bids[0][0] == 1_000_000
        assert result.bids[0][1] == 5
        assert result.asks[0][0] == 1_005_000
        assert result.asks[0][1] == 3


# ===================================================================
# 6. normalize_tick basic
# ===================================================================


class TestNormalizeTickBasic:
    """Standard tick normalization with scaled int output."""

    def test_normalize_tick_basic(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "close": 550.0,
            "volume": 42,
            "total_volume": 10000,
            "ts": 1_620_000_000_000_000,
            "simtrade": 0,
            "intraday_odd": 0,
        }
        result = norm.normalize_tick(payload)

        assert isinstance(result, TickEvent)
        assert result.symbol == "2330"
        assert result.price == 5_500_000  # 550.0 * 10000
        assert result.volume == 42
        assert result.total_volume == 10000
        assert result.is_simtrade is False
        assert result.is_odd_lot is False
        assert result.meta.topic == "tick"


# ===================================================================
# 7. normalize_tick zero volume
# ===================================================================


class TestNormalizeTickZeroVolume:
    """Volume=0 tick should be handled without error."""

    def test_normalize_tick_zero_volume(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "close": 100.0,
            "volume": 0,
            "ts": 1_000_000_000,
        }
        result = norm.normalize_tick(payload)

        assert isinstance(result, TickEvent)
        assert result.price == 1_000_000
        assert result.volume == 0


# ===================================================================
# 8. Snapshot fallback path
# ===================================================================


class TestSnapshotFallbackPath:
    """When snapshot-specific fields (buy_price/sell_price) are missing,
    normalize_snapshot falls back to normalize_bidask and sets is_snapshot."""

    def test_snapshot_fallback_path(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        # No buy_price/sell_price -- should delegate to normalize_bidask
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [100.0, 99.5],
            "bid_volume": [10, 20],
            "ask_price": [100.5],
            "ask_volume": [15],
        }
        result = norm.normalize_snapshot(payload)

        assert isinstance(result, BidAskEvent)
        assert result.is_snapshot is True
        assert len(result.bids) == 2
        assert len(result.asks) == 1
        assert result.bids[0][0] == 1_000_000
        assert result.asks[0][0] == 1_005_000


# ===================================================================
# 9. Scratch array path (HFT_MD_FIXED5_SCRATCH=1)
# ===================================================================


class TestScratchArrayPath:
    """With HFT_MD_FIXED5_SCRATCH=1, verify scratch arrays are pre-allocated."""

    def test_scratch_array_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HFT_MD_FIXED5_SCRATCH", "1")
        monkeypatch.setenv("HFT_FUSED_NORMALIZER", "0")

        cfg_path = tmp_path / "symbols.yaml"
        symbols = [{"code": "2330", "exchange": "TSE", "price_scale": 10000}]
        cfg_path.write_text(yaml.dump({"symbols": symbols}))

        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            # Enable scratch but keep Rust NP function available (mock it)
            with (
                patch("hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH", True),
                patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP", lambda *a, **kw: None),
            ):
                norm = MarketDataNormalizer(config_path=str(cfg_path))

        # When _RUST_NORMALIZE_BIDASK_NP is not None and _SHIOAJI_FIXED5_SCRATCH is True,
        # scratch arrays should be allocated
        assert norm._fixed5_scratch_enabled is True
        assert norm._fixed5_bid_prices_np is not None
        assert norm._fixed5_bid_prices_np.shape == (5,)
        assert norm._fixed5_bid_prices_np.dtype == np.float64
        assert norm._fixed5_bid_vols_np is not None
        assert norm._fixed5_bid_vols_np.dtype == np.int64
        assert norm._fixed5_ask_prices_np is not None
        assert norm._fixed5_ask_vols_np is not None


# ===================================================================
# 10. Python fallback when Rust unavailable
# ===================================================================


class TestPythonFallbackWhenRustUnavailable:
    """Force Python path by disabling Rust, verify correct output."""

    def test_python_fallback_when_rust_unavailable(self, tmp_path, monkeypatch):
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)

        # Tick path
        tick_payload = {
            "code": "2330",
            "close": 250.5,
            "volume": 7,
            "total_volume": 500,
            "ts": 1_000_000_000,
            "simtrade": 0,
            "intraday_odd": 0,
        }
        tick_result = norm.normalize_tick(tick_payload)
        assert isinstance(tick_result, TickEvent)
        assert tick_result.price == 2_505_000  # 250.5 * 10000
        assert tick_result.volume == 7

        # BidAsk path
        bidask_payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [250.0, 249.5],
            "bid_volume": [10, 20],
            "ask_price": [250.5, 251.0],
            "ask_volume": [15, 25],
        }
        bidask_result = norm.normalize_bidask(bidask_payload)
        assert isinstance(bidask_result, BidAskEvent)
        assert len(bidask_result.bids) == 2
        assert len(bidask_result.asks) == 2
        assert bidask_result.bids[0][0] == 2_500_000  # 250.0 * 10000
        assert bidask_result.asks[0][0] == 2_505_000  # 250.5 * 10000
        # Verify the event is well-formed regardless of which internal path computed stats
        assert bidask_result.symbol == "2330"
        assert bidask_result.fused_stats is None  # fused path is disabled
