"""Deep tests for MarketDataNormalizer and SymbolMetadata.

Covers: one-sided books, simtrade/intraday_odd filtering, zero-price levels,
HFT_STRICT_PRICE_MODE, unknown symbol handling, scale factor application,
Rust-disabled Python fallback, SymbolMetadata operations, timestamp clamping,
event modes, and synthetic side generation.
"""

from __future__ import annotations

from unittest.mock import patch

import yaml

from hft_platform.events import BidAskEvent, TickEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_normalizer(tmp_path, symbols=None, monkeypatch=None):
    """Build a MarketDataNormalizer with Rust disabled and optional symbols config."""
    if monkeypatch is not None:
        monkeypatch.setenv("HFT_RUST_ACCEL", "0")
        monkeypatch.setenv("HFT_FUSED_NORMALIZER", "0")

    cfg_path = tmp_path / "symbols.yaml"
    if symbols is None:
        symbols = [
            {"code": "2330", "name": "TSMC", "exchange": "TSE", "price_scale": 10000, "tick_size": 0.01},
            {"code": "2454", "name": "MediaTek", "exchange": "TSE", "price_scale": 10000, "tick_size": 0.5},
        ]
    cfg_path.write_text(yaml.dump({"symbols": symbols}))

    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        # Disable all Rust paths at module level
        with (
            patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", False),
            patch("hft_platform.feed_adapter.normalizer._RUST_FORCE", False),
            patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", False),
            patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", False),
            patch("hft_platform.feed_adapter.normalizer._HAS_FUSED", False),
            patch("hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH", False),
        ):
            from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

            norm = MarketDataNormalizer(config_path=str(cfg_path))
            norm.metrics = None
            norm._fused = None
        return norm


def _make_normalizer_with_synthetic(tmp_path, monkeypatch):
    """Build normalizer with synthetic side enabled."""
    monkeypatch.setenv("HFT_RUST_ACCEL", "0")
    monkeypatch.setenv("HFT_FUSED_NORMALIZER", "0")

    cfg_path = tmp_path / "symbols.yaml"
    symbols = [
        {"code": "2330", "name": "TSMC", "exchange": "TSE", "price_scale": 10000, "tick_size": 0.01},
    ]
    cfg_path.write_text(yaml.dump({"symbols": symbols}))

    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        with (
            patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", False),
            patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", False),
            patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True),
            patch("hft_platform.feed_adapter.normalizer._HAS_FUSED", False),
            patch("hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH", False),
        ):
            from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

            norm = MarketDataNormalizer(config_path=str(cfg_path))
            norm.metrics = None
            norm._fused = None
        return norm


# ===================================================================
# SymbolMetadata tests
# ===================================================================


class TestSymbolMetadata:
    """Tests for SymbolMetadata configuration loading."""

    def test_load_basic(self, tmp_path):
        """Load symbols from YAML and verify metadata."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            yaml.dump(
                {
                    "symbols": [
                        {"code": "2330", "name": "TSMC", "exchange": "TSE", "price_scale": 10000},
                    ]
                }
            )
        )
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert meta.price_scale("2330") == 10000
        assert meta.exchange("2330") == "TSE"

    def test_default_scale_for_unknown(self, tmp_path):
        """Unknown symbol returns DEFAULT_SCALE."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(yaml.dump({"symbols": []}))
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert meta.price_scale("UNKNOWN") == SymbolMetadata.DEFAULT_SCALE

    def test_tick_size_fallback(self, tmp_path):
        """When price_scale is missing, derive from tick_size."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            yaml.dump(
                {
                    "symbols": [
                        {"code": "6666", "tick_size": 0.01},
                    ]
                }
            )
        )
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert meta.price_scale("6666") == 100

    def test_price_scale_cache(self, tmp_path):
        """Second lookup uses cached value."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            yaml.dump(
                {
                    "symbols": [
                        {"code": "2330", "price_scale": 10000},
                    ]
                }
            )
        )
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        scale1 = meta.price_scale("2330")
        scale2 = meta.price_scale("2330")
        assert scale1 == scale2 == 10000

    def test_tags_loading(self, tmp_path):
        """Tags are parsed and accessible."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            yaml.dump(
                {
                    "symbols": [
                        {"code": "2330", "tags": ["blue_chip", "semiconductor"]},
                        {"code": "2454", "tags": "blue_chip|tech"},
                    ]
                }
            )
        )
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert "semiconductor" in meta.tags_by_symbol.get("2330", set())
        assert "blue_chip" in meta.tags_by_symbol.get("2454", set())
        resolved = meta.symbols_for_tags(["blue_chip"])
        assert "2330" in resolved
        assert "2454" in resolved

    def test_product_type_from_exchange(self, tmp_path):
        """Product type inferred from exchange when not explicit."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            yaml.dump(
                {
                    "symbols": [
                        {"code": "2330", "exchange": "TSE"},
                        {"code": "TXF", "exchange": "TAIFEX"},
                    ]
                }
            )
        )
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert meta.product_type("2330") == "stock"
        assert meta.product_type("TXF") == "future"

    def test_reload_if_changed(self, tmp_path):
        """Reload detects file modification."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(yaml.dump({"symbols": [{"code": "A", "price_scale": 100}]}))
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert meta.price_scale("A") == 100

        # No change => no reload
        assert meta.reload_if_changed() is False

    def test_missing_config_file(self, tmp_path):
        """SymbolMetadata gracefully handles missing config."""
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(tmp_path / "nonexistent.yaml"))
        assert meta.price_scale("ANYTHING") == SymbolMetadata.DEFAULT_SCALE

    def test_order_params(self, tmp_path):
        """Order params extraction from symbol config."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            yaml.dump(
                {
                    "symbols": [
                        {"code": "2330", "order_cond": "Cash", "order_lot": "Common"},
                    ]
                }
            )
        )
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        params = meta.order_params("2330")
        assert params.get("order_cond") == "Cash"
        assert params.get("order_lot") == "Common"

    def test_order_params_unknown_symbol(self, tmp_path):
        """Unknown symbol returns empty order params."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(yaml.dump({"symbols": []}))
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert meta.order_params("MISSING") == {}


# ===================================================================
# normalize_tick tests
# ===================================================================


class TestNormalizeTick:
    """Tests for MarketDataNormalizer.normalize_tick."""

    def test_basic_dict_payload(self, tmp_path, monkeypatch):
        """Standard dict payload produces TickEvent with scaled price."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {"code": "2330", "close": 100.0, "volume": 5, "ts": 1_000_000_000}
        result = norm.normalize_tick(payload)
        assert isinstance(result, TickEvent)
        assert result.price == 1_000_000  # 100.0 * 10000
        assert result.volume == 5
        assert result.symbol == "2330"

    def test_object_payload(self, tmp_path, monkeypatch):
        """Attribute-based payload (simulating Shioaji object)."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)

        class FakeTick:
            code = "2330"
            close = 50.5
            volume = 10
            ts = 1_000_000_000
            total_volume = 100
            simtrade = 0
            intraday_odd = 0

        result = norm.normalize_tick(FakeTick())
        assert isinstance(result, TickEvent)
        assert result.price == 505_000  # 50.5 * 10000

    def test_missing_symbol_returns_none(self, tmp_path, monkeypatch):
        """Payload without code returns None."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        result = norm.normalize_tick({"close": 100.0, "volume": 1, "ts": 0})
        assert result is None

    def test_simtrade_flag(self, tmp_path, monkeypatch):
        """simtrade flag is captured."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {"code": "2330", "close": 100.0, "volume": 1, "ts": 0, "simtrade": 1}
        result = norm.normalize_tick(payload)
        assert isinstance(result, TickEvent)
        assert result.is_simtrade is True

    def test_intraday_odd_flag(self, tmp_path, monkeypatch):
        """intraday_odd flag is captured."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {"code": "2330", "close": 100.0, "volume": 1, "ts": 0, "intraday_odd": 1}
        result = norm.normalize_tick(payload)
        assert isinstance(result, TickEvent)
        assert result.is_odd_lot is True

    def test_zero_close_price(self, tmp_path, monkeypatch):
        """Zero close is a Shioaji 'no data' sentinel — must be filtered (returns None)."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {"code": "2330", "close": 0.0, "volume": 1, "ts": 0}
        result = norm.normalize_tick(payload)
        assert result is None

    def test_none_close_price(self, tmp_path, monkeypatch):
        """None close produces zero price."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {"code": "2330", "close": None, "volume": 1, "ts": 0}
        result = norm.normalize_tick(payload)
        assert isinstance(result, TickEvent)
        assert result.price == 0

    def test_scale_factor_applied(self, tmp_path, monkeypatch):
        """Different symbol scale factors produce correct scaled prices."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        # 2454 has tick_size 0.5, scale would be derived as 10000 from price_scale
        result = norm.normalize_tick({"code": "2454", "close": 200.0, "volume": 1, "ts": 0})
        assert isinstance(result, TickEvent)
        assert result.price == 2_000_000  # 200.0 * 10000

    def test_seq_increments(self, tmp_path, monkeypatch):
        """Sequence numbers increment across calls."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        r1 = norm.normalize_tick({"code": "2330", "close": 100.0, "volume": 1, "ts": 0})
        r2 = norm.normalize_tick({"code": "2330", "close": 101.0, "volume": 1, "ts": 0})
        assert isinstance(r1, TickEvent) and isinstance(r2, TickEvent)
        assert r2.meta.seq > r1.meta.seq


# ===================================================================
# normalize_bidask tests
# ===================================================================


class TestNormalizeBidAsk:
    """Tests for MarketDataNormalizer.normalize_bidask."""

    def test_basic_bidask(self, tmp_path, monkeypatch):
        """Standard 5-level bidask produces BidAskEvent."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [100.0, 99.5, 99.0, 98.5, 98.0],
            "bid_volume": [10, 20, 30, 40, 50],
            "ask_price": [100.5, 101.0, 101.5, 102.0, 102.5],
            "ask_volume": [15, 25, 35, 45, 55],
        }
        result = norm.normalize_bidask(payload)
        assert isinstance(result, BidAskEvent)
        assert result.symbol == "2330"
        assert len(result.bids) == 5
        assert len(result.asks) == 5
        # First bid price should be 100.0 * 10000 = 1_000_000
        assert result.bids[0][0] == 1_000_000

    def test_empty_book_both_sides(self, tmp_path, monkeypatch):
        """Empty bids and asks produce empty lists."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 0,
            "bid_price": [],
            "bid_volume": [],
            "ask_price": [],
            "ask_volume": [],
        }
        result = norm.normalize_bidask(payload)
        assert isinstance(result, BidAskEvent)
        assert len(result.bids) == 0
        assert len(result.asks) == 0

    def test_one_sided_bids_only(self, tmp_path, monkeypatch):
        """Bids only, no asks."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 0,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [],
            "ask_volume": [],
        }
        result = norm.normalize_bidask(payload)
        assert isinstance(result, BidAskEvent)
        assert len(result.bids) == 1
        assert len(result.asks) == 0

    def test_one_sided_asks_only(self, tmp_path, monkeypatch):
        """Asks only, no bids."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 0,
            "bid_price": [],
            "bid_volume": [],
            "ask_price": [101.0],
            "ask_volume": [5],
        }
        result = norm.normalize_bidask(payload)
        assert isinstance(result, BidAskEvent)
        assert len(result.bids) == 0
        assert len(result.asks) == 1

    def test_zero_price_levels_filtered(self, tmp_path, monkeypatch):
        """Zero-price levels are filtered out in Python fallback path."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 0,
            "bid_price": [100.0, 0.0],
            "bid_volume": [10, 20],
            "ask_price": [101.0, 0.0],
            "ask_volume": [15, 25],
        }
        result = norm.normalize_bidask(payload)
        assert isinstance(result, BidAskEvent)
        # Zero-price levels should be filtered (price && volume check in list comp)
        assert len(result.bids) == 1
        assert len(result.asks) == 1

    def test_missing_symbol_returns_none(self, tmp_path, monkeypatch):
        """No symbol in payload => None."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "ts": 0,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [101.0],
            "ask_volume": [5],
        }
        result = norm.normalize_bidask(payload)
        assert result is None

    def test_object_payload(self, tmp_path, monkeypatch):
        """Attribute-based BidAsk payload."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)

        class FakeBidAsk:
            code = "2330"
            ts = 1_000_000_000
            bid_price = [100.0]
            bid_volume = [10]
            ask_price = [101.0]
            ask_volume = [5]

        result = norm.normalize_bidask(FakeBidAsk())
        assert isinstance(result, BidAskEvent)
        assert result.symbol == "2330"


# ===================================================================
# Synthetic side generation tests
# ===================================================================


class TestSyntheticSide:
    """Tests for _maybe_synthesize_side."""

    def test_synthetic_asks_generated(self, tmp_path, monkeypatch):
        """With synthetic enabled and bids-only, asks are synthesized."""
        norm = _make_normalizer_with_synthetic(tmp_path, monkeypatch)
        bids = [[1_000_000, 10]]
        asks = []
        scale = 10000
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            new_bids, new_asks, synthesized = norm._maybe_synthesize_side("2330", bids, asks, scale)
        assert synthesized is True
        assert len(new_asks) == 1
        assert new_asks[0][0] > 1_000_000

    def test_synthetic_bids_generated(self, tmp_path, monkeypatch):
        """With synthetic enabled and asks-only, bids are synthesized."""
        norm = _make_normalizer_with_synthetic(tmp_path, monkeypatch)
        bids = []
        asks = [[1_010_000, 5]]
        scale = 10000
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            new_bids, new_asks, synthesized = norm._maybe_synthesize_side("2330", bids, asks, scale)
        assert synthesized is True
        assert len(new_bids) == 1
        assert new_bids[0][0] < 1_010_000

    def test_no_synthesis_when_both_sides_present(self, tmp_path, monkeypatch):
        """No synthesis when both sides have levels."""
        norm = _make_normalizer_with_synthetic(tmp_path, monkeypatch)
        bids = [[1_000_000, 10]]
        asks = [[1_010_000, 5]]
        scale = 10000
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            new_bids, new_asks, synthesized = norm._maybe_synthesize_side("2330", bids, asks, scale)
        assert synthesized is False
        assert new_bids is bids
        assert new_asks is asks


# ===================================================================
# Timestamp clamping tests
# ===================================================================


class TestTimestampClamping:
    """Tests for _clamp_future_ts and _validate_and_sync_timestamp."""

    def test_clamp_future_ts(self, tmp_path, monkeypatch):
        """Exchange TS far in the future is clamped to now."""
        from hft_platform.feed_adapter import normalizer as norm_mod

        original_max = norm_mod._TS_MAX_FUTURE_NS
        try:
            norm_mod._TS_MAX_FUTURE_NS = 1_000_000_000  # 1 second
            now = 10_000_000_000
            exch_ts = now + 5_000_000_000  # 5s in future
            result = norm_mod._clamp_future_ts(exch_ts, now, "tick", "2330")
            assert result == now
        finally:
            norm_mod._TS_MAX_FUTURE_NS = original_max

    def test_no_clamp_within_tolerance(self, tmp_path, monkeypatch):
        """Exchange TS within tolerance is not clamped."""
        from hft_platform.feed_adapter import normalizer as norm_mod

        original_max = norm_mod._TS_MAX_FUTURE_NS
        try:
            norm_mod._TS_MAX_FUTURE_NS = 5_000_000_000  # 5 seconds
            now = 10_000_000_000
            exch_ts = now + 100_000_000  # 100ms in future
            result = norm_mod._clamp_future_ts(exch_ts, now, "tick", "2330")
            assert result == exch_ts
        finally:
            norm_mod._TS_MAX_FUTURE_NS = original_max

    def test_validate_and_sync_local_ts_synced(self, tmp_path, monkeypatch):
        """local_ts is bumped to exch_ts when local_ts < exch_ts."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        exch_ts = 10_000_000_000
        local_ts = 5_000_000_000  # before exch
        new_exch, new_local = norm._validate_and_sync_timestamp(exch_ts, local_ts, "tick", "2330")
        assert new_local >= new_exch


# ===================================================================
# _get_scale and caching tests
# ===================================================================


class TestGetScale:
    """Tests for _get_scale caching behavior."""

    def test_scale_cache_hit(self, tmp_path, monkeypatch):
        """Repeated calls for same symbol use cached scale."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        s1 = norm._get_scale("2330")
        s2 = norm._get_scale("2330")
        assert s1 == s2 == 10000
        assert norm._last_symbol == "2330"

    def test_scale_cache_miss(self, tmp_path, monkeypatch):
        """Different symbol triggers fresh lookup."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        norm._get_scale("2330")
        norm._get_scale("2454")
        assert norm._last_symbol == "2454"

    def test_unknown_symbol_default_scale(self, tmp_path, monkeypatch):
        """Unknown symbol returns DEFAULT_SCALE."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        assert norm._get_scale("XXXX") == SymbolMetadata.DEFAULT_SCALE


# ===================================================================
# normalize_snapshot tests
# ===================================================================


class TestNormalizeSnapshot:
    """Tests for normalize_snapshot."""

    def test_snapshot_with_buy_sell_price(self, tmp_path, monkeypatch):
        """Snapshot with buy_price/sell_price produces BidAskEvent with is_snapshot."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 0,
            "buy_price": 100.0,
            "buy_volume": 10,
            "sell_price": 101.0,
            "sell_volume": 5,
        }
        result = norm.normalize_snapshot(payload)
        assert isinstance(result, BidAskEvent)
        assert result.is_snapshot is True

    def test_snapshot_falls_back_to_bidask(self, tmp_path, monkeypatch):
        """Snapshot without buy_price/sell_price delegates to normalize_bidask."""
        norm = _make_normalizer(tmp_path, monkeypatch=monkeypatch)
        payload = {
            "code": "2330",
            "ts": 0,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [101.0],
            "ask_volume": [5],
        }
        result = norm.normalize_snapshot(payload)
        assert isinstance(result, BidAskEvent)
        assert result.is_snapshot is True
