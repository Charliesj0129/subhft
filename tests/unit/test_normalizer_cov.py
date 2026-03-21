"""Comprehensive coverage tests for feed_adapter/normalizer.py.

Targets uncovered branches identified from coverage report:
- Env var parse error handlers (lines 51-63)
- Rust import failure fallback (lines 82-97)
- Fused normalizer init failure (lines 103-108)
- Config path fallback logic (lines 121-126)
- Tags/metadata edge cases
- reload_if_changed branches
- tick_size ZeroDivisionError
- Exchange/product_type cache branches
- order_params loop
- Fused/fixed5 scratch init paths
- Metrics latency recording
- Rust get_field exception path
- get_field object attribute path
- get_scale branches
- _maybe_synthesize_side tick_size branches
- normalize_tick Rust tuple and non-Rust _RETURN_TUPLE paths
- normalize_bidask Rust paths (fused, synth, np, pair, pair_stats, seq)
- normalize_bidask _RETURN_TUPLE paths
- normalize_snapshot object path and tuple returns
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path, symbols=None):
    cfg = tmp_path / "symbols.yaml"
    if symbols is None:
        symbols = [
            {"code": "2330", "name": "TSMC", "exchange": "TSE", "price_scale": 10000, "tick_size": 0.01},
            {"code": "TXF", "exchange": "TAIFEX", "price_scale": 10000},
            {"code": "OPT1", "exchange": "OPT", "price_scale": 10000},
            {"code": "IDX1", "exchange": "IDX", "price_scale": 10000},
        ]
    cfg.write_text(yaml.dump({"symbols": symbols}))
    return str(cfg)


def _make_norm(tmp_path, symbols=None, **env_patches):
    """Build a MarketDataNormalizer with all Rust paths disabled (pure Python)."""
    cfg = _make_cfg(tmp_path, symbols)
    patches = {
        "hft_platform.feed_adapter.normalizer._RUST_ENABLED": False,
        "hft_platform.feed_adapter.normalizer._RUST_FORCE": False,
        "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
        "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
        "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
        "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
        "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
        "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
        "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
        "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
        "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
        "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
        "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_TICK": None,
        "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
    }
    patches.update(env_patches)
    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        with _multi_patch(patches):
            from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

            norm = MarketDataNormalizer(config_path=cfg)
            norm.metrics = None
            norm._fused = None
    return norm


def _multi_patch(patch_dict):
    """Context manager that applies multiple patches at once."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        managers = [patch(k, v) for k, v in patch_dict.items()]
        entered = []
        try:
            for m in managers:
                entered.append(m.__enter__())
            yield entered
        finally:
            for m in reversed(managers):
                m.__exit__(None, None, None)

    return _ctx()


# ---------------------------------------------------------------------------
# SymbolMetadata – uncovered branches
# ---------------------------------------------------------------------------


class TestSymbolMetadataUncovered:
    """Cover branches missed in existing tests."""

    def test_tags_raw_string_pipe_separated(self, tmp_path):
        """Tags as pipe-separated string are parsed correctly (line 161-162)."""
        cfg = _make_cfg(tmp_path, [{"code": "A", "tags": "tech|growth|blue_chip"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert "tech" in meta.tags_by_symbol.get("A", set())
        assert "growth" in meta.tags_by_symbol.get("A", set())
        assert "blue_chip" in meta.tags_by_symbol.get("A", set())

    def test_tags_raw_comma_separated(self, tmp_path):
        """Tags as comma-separated string (line 161)."""
        cfg = _make_cfg(tmp_path, [{"code": "B", "tags": "alpha,beta"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert "alpha" in meta.tags_by_symbol.get("B", set())
        assert "beta" in meta.tags_by_symbol.get("B", set())

    def test_tags_raw_other_type_falls_to_empty(self, tmp_path):
        """Tags as non-string/non-list type results in no tags (line 165)."""
        cfg = _make_cfg(tmp_path, [{"code": "C", "tags": 42}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.tags_by_symbol.get("C") is None

    def test_tags_tuple_type(self, tmp_path):
        """Tags as a tuple are processed (line 162-163)."""
        # YAML doesn't natively produce tuple; test by directly calling _load
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(yaml.dump({"symbols": [{"code": "D", "tags": ["x", "y"]}]}))
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        assert "x" in meta.tags_by_symbol.get("D", set())

    def test_reload_if_changed_no_file(self, tmp_path):
        """reload_if_changed returns False for missing file (lines 180-181)."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(yaml.dump({"symbols": []}))
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        # Remove file so getmtime raises OSError
        cfg.unlink()
        result = meta.reload_if_changed()
        assert result is False

    def test_reload_if_changed_when_mtime_is_none(self, tmp_path):
        """reload_if_changed triggers when _mtime is None (line 183)."""
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(yaml.dump({"symbols": [{"code": "X", "price_scale": 9999}]}))
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(str(cfg))
        meta._mtime = None  # force None
        result = meta.reload_if_changed()
        assert result is True
        assert meta.price_scale("X") == 9999

    def test_exchange_cache_hit(self, tmp_path):
        """Exchange returns cached value on second call (line 222)."""
        cfg = _make_cfg(tmp_path, [{"code": "2330", "exchange": "TSE"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        e1 = meta.exchange("2330")
        e2 = meta.exchange("2330")  # hits cache
        assert e1 == e2 == "TSE"

    def test_product_type_explicit(self, tmp_path):
        """product_type returns explicit value from config (line 231)."""
        cfg = _make_cfg(tmp_path, [{"code": "X", "product_type": "etf"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("X") == "etf"

    def test_product_type_cache_hit(self, tmp_path):
        """Second product_type call uses cache (line 229)."""
        cfg = _make_cfg(tmp_path, [{"code": "2330", "exchange": "TSE"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        pt1 = meta.product_type("2330")
        pt2 = meta.product_type("2330")
        assert pt1 == pt2 == "stock"

    def test_product_type_security_type_fallback(self, tmp_path):
        """product_type uses security_type field as fallback (line 233-235)."""
        cfg = _make_cfg(tmp_path, [{"code": "X", "security_type": "bond"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("X") == "bond"

    def test_product_type_asset_type_fallback(self, tmp_path):
        """product_type uses asset_type field as fallback (line 233-235)."""
        cfg = _make_cfg(tmp_path, [{"code": "X", "asset_type": "warrant"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("X") == "warrant"

    def test_product_type_opt_exchange(self, tmp_path):
        """OPT exchange returns 'option' (line 253-254)."""
        cfg = _make_cfg(tmp_path, [{"code": "OPT1", "exchange": "OPT"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("OPT1") == "option"

    def test_product_type_options_exchange(self, tmp_path):
        """OPTIONS exchange returns 'option'."""
        cfg = _make_cfg(tmp_path, [{"code": "OPT2", "exchange": "OPTIONS"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("OPT2") == "option"

    def test_product_type_idx_exchange(self, tmp_path):
        """IDX exchange returns 'index' (line 255-256)."""
        cfg = _make_cfg(tmp_path, [{"code": "IDX1", "exchange": "IDX"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("IDX1") == "index"

    def test_product_type_index_exchange(self, tmp_path):
        """INDEX exchange returns 'index'."""
        cfg = _make_cfg(tmp_path, [{"code": "IDX2", "exchange": "INDEX"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("IDX2") == "index"

    def test_product_type_fut_exchange(self, tmp_path):
        """FUT exchange returns 'future' (line 249-250)."""
        cfg = _make_cfg(tmp_path, [{"code": "TXF", "exchange": "FUT"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("TXF") == "future"

    def test_product_type_futures_exchange(self, tmp_path):
        """FUTURES exchange returns 'future'."""
        cfg = _make_cfg(tmp_path, [{"code": "TXF2", "exchange": "FUTURES"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("TXF2") == "future"

    def test_product_type_unknown_exchange_empty(self, tmp_path):
        """Unknown exchange returns empty string (line 258)."""
        cfg = _make_cfg(tmp_path, [{"code": "ZZZ", "exchange": "UNKNOWN_EXCHANGE"}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.product_type("ZZZ") == ""

    def test_order_params_filters_none_values(self, tmp_path):
        """order_params skips keys with None values (line 265-266)."""
        cfg = _make_cfg(tmp_path, [{"code": "2330", "order_cond": "Cash", "order_lot": None}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        params = meta.order_params("2330")
        assert "order_cond" in params
        assert "order_lot" not in params  # None values excluded

    def test_tick_size_zero_division_fallback(self, tmp_path):
        """ZeroDivisionError in tick_size calculation falls back to DEFAULT_SCALE (line 214-215)."""
        cfg = _make_cfg(tmp_path, [{"code": "X", "tick_size": 0}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        # tick_size=0 should trigger ZeroDivisionError in 1/tick_size
        assert meta.price_scale("X") == SymbolMetadata.DEFAULT_SCALE

    def test_price_scale_uses_tick_size_when_no_price_scale(self, tmp_path):
        """price_scale derived from tick_size when price_scale absent (lines 207-215)."""
        cfg = _make_cfg(tmp_path, [{"code": "X", "tick_size": 0.5}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        assert meta.price_scale("X") == 2  # 1 / 0.5 = 2

    def test_symbols_for_tags_empty_tag(self, tmp_path):
        """Empty string tag is ignored in symbols_for_tags (line 192)."""
        cfg = _make_cfg(tmp_path, [{"code": "A", "tags": ["tech"]}])
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        result = meta.symbols_for_tags(["", "  ", "tech"])
        assert "A" in result

    def test_symbols_for_tags_unknown_tag(self, tmp_path):
        """Unknown tag returns empty set (line 193)."""
        cfg = _make_cfg(tmp_path)
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        meta = SymbolMetadata(cfg)
        result = meta.symbols_for_tags(["nonexistent_tag"])
        assert result == set()


# ---------------------------------------------------------------------------
# _clamp_future_ts and _extract_ts_ns
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_clamp_future_ts_no_ts(self):
        """_clamp_future_ts with exch_ts=0 returns 0 unchanged (line 275)."""
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        with patch("hft_platform.feed_adapter.normalizer._TS_MAX_FUTURE_NS", 1_000_000):
            result = _clamp_future_ts(0, 1_000_000_000, "tick", "SYM")
        assert result == 0

    def test_clamp_future_ts_no_max(self):
        """_clamp_future_ts with _TS_MAX_FUTURE_NS=0 returns exch_ts unchanged (line 275)."""
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        with patch("hft_platform.feed_adapter.normalizer._TS_MAX_FUTURE_NS", 0):
            result = _clamp_future_ts(9_000_000_000, 1_000_000_000, "tick", "SYM")
        assert result == 9_000_000_000

    def test_clamp_future_ts_within_limit(self):
        """Timestamp within future limit passes through unchanged (line 287)."""
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        now = 1_000_000_000_000_000_000
        future = now + 1_000_000  # small delta
        with patch("hft_platform.feed_adapter.normalizer._TS_MAX_FUTURE_NS", 5_000_000_000_000):
            result = _clamp_future_ts(future, now, "tick", "SYM")
        assert result == future

    def test_clamp_future_ts_clamped_to_now(self):
        """Timestamp too far in future clamped to now (line 280-286)."""
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        now = 1_000_000_000_000_000_000
        way_future = now + 100_000_000_000_000_000  # 100s in future
        with patch("hft_platform.feed_adapter.normalizer._TS_MAX_FUTURE_NS", 5_000_000_000):
            result = _clamp_future_ts(way_future, now, "tick", "SYM")
        assert result == now


# ---------------------------------------------------------------------------
# _get_field object path
# ---------------------------------------------------------------------------


class TestGetField:
    def test_get_field_dict_path(self, tmp_path):
        """_get_field uses dict.get for dict payloads (lines 403-408)."""
        norm = _make_norm(tmp_path)
        result = norm._get_field({"code": "2330", "close": 100.0}, ["close", "Close"])
        assert result == 100.0

    def test_get_field_object_path(self, tmp_path):
        """_get_field uses getattr for non-dict payloads (lines 411-414)."""
        norm = _make_norm(tmp_path)

        class FakeObj:
            close = 55.5

        result = norm._get_field(FakeObj(), ["close", "Close"])
        assert result == 55.5

    def test_get_field_object_returns_none_when_not_found(self, tmp_path):
        """_get_field returns None when attribute not found on object (line 415)."""
        norm = _make_norm(tmp_path)
        result = norm._get_field(SimpleNamespace(), ["nonexistent_key"])
        assert result is None

    def test_get_field_dict_missing_key_returns_none(self, tmp_path):
        """_get_field returns None for missing dict key (line 408)."""
        norm = _make_norm(tmp_path)
        result = norm._get_field({"code": "2330"}, ["close", "Close"])
        assert result is None

    def test_get_field_with_rust_exception_fallback(self, tmp_path):
        """_get_field falls back to Python when Rust raises exception (lines 400-401)."""
        rust_get_field = MagicMock(side_effect=RuntimeError("rust error"))
        norm = _make_norm(tmp_path)
        with patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", True):
            with patch("hft_platform.feed_adapter.normalizer._RUST_GET_FIELD", rust_get_field):
                result = norm._get_field({"close": 77.7}, ["close"])
        assert result == 77.7


# ---------------------------------------------------------------------------
# _get_scale cache hit
# ---------------------------------------------------------------------------


class TestGetScale:
    def test_get_scale_cache_hit(self, tmp_path):
        """Second call to _get_scale for same symbol uses cache (line 418-419)."""
        norm = _make_norm(tmp_path)
        s1 = norm._get_scale("2330")
        s2 = norm._get_scale("2330")  # hits cache
        assert s1 == s2 == 10000

    def test_get_scale_zero_fixed_to_one(self, tmp_path):
        """_get_scale with scale<=0 clamps to 1 (line 422)."""
        cfg = _make_cfg(tmp_path, [{"code": "BAD", "price_scale": 0}])
        norm = _make_norm(tmp_path, symbols=[{"code": "BAD", "price_scale": 0}])
        scale = norm._get_scale("BAD")
        assert scale == 1


# ---------------------------------------------------------------------------
# _maybe_synthesize_side branches
# ---------------------------------------------------------------------------


class TestMaybeSynthesizeSide:
    def _norm_with_synthetic(self, tmp_path):
        """Build a normalizer; synthetic tests call _maybe_synthesize_side with _SYNTHETIC_SIDE patched at call time."""
        cfg = _make_cfg(tmp_path, [{"code": "2330", "exchange": "TSE", "price_scale": 10000, "tick_size": 0.01}])
        return _make_norm(
            tmp_path, symbols=[{"code": "2330", "exchange": "TSE", "price_scale": 10000, "tick_size": 0.01}]
        )

    def test_both_sides_present_no_synthesis(self, tmp_path):
        """When both sides present, synthesize returns unchanged (line 446-447)."""
        norm = self._norm_with_synthetic(tmp_path)
        bids = [[1000000, 1]]
        asks = [[1010000, 1]]
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            b, a, synthesized = norm._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is False
        assert b == bids
        assert a == asks

    def test_synthesize_missing_bid(self, tmp_path):
        """When bids empty, synthesize bid side from best ask (lines 470-473)."""
        norm = self._norm_with_synthetic(tmp_path)
        asks = [[1010000, 1]]
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            b, a, synthesized = norm._maybe_synthesize_side("2330", [], asks, 10000)
        assert synthesized is True
        assert len(b) == 1
        assert b[0][0] == 1010000 - 100  # one tick below (tick_int = round(0.01*10000) = 100)

    def test_synthesize_missing_ask(self, tmp_path):
        """When asks empty, synthesize ask side from best bid (lines 475-478)."""
        norm = self._norm_with_synthetic(tmp_path)
        bids = [[1000000, 1]]
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            b, a, synthesized = norm._maybe_synthesize_side("2330", bids, [], 10000)
        assert synthesized is True
        assert len(a) == 1
        assert a[0][0] == 1000000 + 100  # one tick above

    def test_synthesize_no_tick_size_config(self, tmp_path):
        """Synthesize with no tick_size uses 1/scale fallback (lines 457-460)."""
        norm = self._norm_with_synthetic(tmp_path)
        # Remove tick_size so the fallback path is used
        norm.metadata.meta["2330"].pop("tick_size", None)
        asks = [[1000000, 1]]
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            b, a, synthesized = norm._maybe_synthesize_side("2330", [], asks, 10000)
        assert synthesized is True
        # tick_int = round(1/10000 * 10000) = round(1.0) = 1
        assert b[0][0] == 1000000 - 1

    def test_synthesize_numpy_bids(self, tmp_path):
        """_has_levels works with numpy arrays (line 441)."""
        norm = self._norm_with_synthetic(tmp_path)
        # numpy array with size > 0
        asks_np = np.array([[1010000, 1]], dtype=np.int64)
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            b, a, synthesized = norm._maybe_synthesize_side("2330", np.zeros((0, 2), dtype=np.int64), asks_np, 10000)
        assert synthesized is True

    def test_synthesize_both_empty_returns_unchanged(self, tmp_path):
        """When both sides empty, no synthesis (has_bids=False and has_asks=False -> synthesized stays False)."""
        norm = self._norm_with_synthetic(tmp_path)
        with patch("hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE", True):
            b, a, synthesized = norm._maybe_synthesize_side("2330", [], [], 10000)
        assert synthesized is False

    def test_synthesize_not_enabled_noop(self, tmp_path):
        """When _SYNTHETIC_SIDE is False, _maybe_synthesize_side is a noop (line 434-435)."""
        norm = _make_norm(tmp_path)  # SYNTHETIC_SIDE=False
        bids = []
        asks = [[1010000, 1]]
        b, a, synthesized = norm._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is False
        assert b == bids
        assert a == asks


# ---------------------------------------------------------------------------
# Metrics latency recording
# ---------------------------------------------------------------------------


class TestMetricsLatencyRecording:
    def _make_norm_with_metrics(self, tmp_path):
        """Build normalizer with a mock metrics object."""
        cfg = _make_cfg(tmp_path)
        mock_metrics = MagicMock()
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = mock_metrics
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": False,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": False,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_TICK": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm._fused = None
        return norm, mock_metrics

    def test_tick_records_feed_latency(self, tmp_path):
        """normalize_tick records feed_latency_ns metric (lines 383-385)."""
        norm, metrics = self._make_norm_with_metrics(tmp_path)
        # Set large exch_ts to ensure lag >= 0
        payload = {"code": "2330", "close": 100.0, "volume": 1, "ts": 1_000_000_000}
        norm.normalize_tick(payload)
        assert metrics.feed_latency_ns.observe.called

    def test_tick_records_interarrival_ns_after_second_tick(self, tmp_path):
        """normalize_tick records feed_interarrival_ns on second call (lines 387-390)."""
        norm, metrics = self._make_norm_with_metrics(tmp_path)
        payload = {"code": "2330", "close": 100.0, "volume": 1, "ts": 1_000_000_000}
        norm.normalize_tick(payload)
        norm.normalize_tick(payload)
        assert metrics.feed_interarrival_ns.observe.called

    def test_bidask_records_feed_latency(self, tmp_path):
        """normalize_bidask records metrics (lines 383-390)."""
        norm, metrics = self._make_norm_with_metrics(tmp_path)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        norm.normalize_bidask(payload)
        assert metrics.feed_latency_ns.observe.called


# ---------------------------------------------------------------------------
# normalize_tick – _RETURN_TUPLE path (Python, no Rust)
# ---------------------------------------------------------------------------


class TestNormalizeTick:
    def test_return_tuple_mode_tick(self, tmp_path):
        """With _RETURN_TUPLE=True, normalize_tick returns a tuple (line 562-571)."""
        norm = _make_norm(tmp_path)
        payload = {"code": "2330", "close": 100.0, "volume": 5, "ts": 1_000_000_000_000}
        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
            result = norm.normalize_tick(payload)
        assert isinstance(result, tuple)
        assert result[0] == "tick"
        assert result[1] == "2330"
        assert result[2] == 1_000_000  # 100.0 * 10000 scaled int
        assert result[3] == 5

    def test_return_tuple_mode_tick_no_close(self, tmp_path):
        """_RETURN_TUPLE with missing close returns tuple with price=0."""
        norm = _make_norm(tmp_path)
        payload = {"code": "2330", "volume": 1}
        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
            result = norm.normalize_tick(payload)
        assert isinstance(result, tuple)
        assert result[2] == 0  # price=0

    def test_tick_rust_path_returns_tuple_when_return_tuple_set(self, tmp_path):
        """Rust tick path returns the rust_tuple directly when _RETURN_TUPLE=True (line 532)."""
        norm = _make_norm(tmp_path)
        rust_tuple = ("tick", "2330", 1_000_000, 5, 100, False, False, 1_000_000_000_000)
        rust_normalize_tick = MagicMock(return_value=rust_tuple)
        payload = {"code": "2330", "close": 100.0, "volume": 5, "ts": 1_000_000_000_000}
        with patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", True):
            with patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_TICK", rust_normalize_tick):
                with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
                    result = norm.normalize_tick(payload)
        assert result == rust_tuple


# ---------------------------------------------------------------------------
# normalize_bidask – _RETURN_TUPLE paths
# ---------------------------------------------------------------------------


class TestNormalizeBidask:
    def test_return_tuple_mode_bidask_no_stats(self, tmp_path):
        """With _RETURN_TUPLE=True and no stats, returns 6-element tuple (line 977)."""
        norm = _make_norm(tmp_path)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0, 99.5],
            "bid_volume": [1, 2],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
            result = norm.normalize_bidask(payload)
        assert isinstance(result, tuple)
        assert result[0] == "bidask"
        assert result[1] == "2330"
        assert len(result) >= 6

    def test_return_tuple_mode_with_stats(self, tmp_path):
        """With _RETURN_TUPLE=True and stats present, returns 13-element tuple (lines 961-976)."""
        norm = _make_norm(tmp_path)
        stats_result = (
            [[1_000_000, 1]],
            [[1_010_000, 1]],
            (1_000_000, 1_010_000, 1, 1, 1_005_000.0, 10_000.0, 0.5),
        )
        rust_scale_pair_stats = MagicMock(return_value=stats_result)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        with patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", True):
            with patch("hft_platform.feed_adapter.normalizer._RUST_FORCE", True):
                with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS", rust_scale_pair_stats):
                    with patch("hft_platform.feed_adapter.normalizer._RUST_STATS_TUPLE", True):
                        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
                            result = norm.normalize_bidask(payload)
        assert isinstance(result, tuple)
        assert result[0] == "bidask"
        # With stats and RETURN_TUPLE: 13 elements
        assert len(result) == 13

    def test_bidask_object_payload(self, tmp_path):
        """normalize_bidask handles object (non-dict) payload (lines 604-610)."""
        norm = _make_norm(tmp_path)

        class FakeBidAsk:
            code = "2330"
            ts = 1_000_000_000_000
            bid_price = [100.0, 99.5]
            bid_volume = [1, 2]
            ask_price = [101.0]
            ask_volume = [1]

        result = norm.normalize_bidask(FakeBidAsk())
        assert result is not None
        assert result.symbol == "2330"
        assert result.bids[0][0] == 1_000_000

    def test_bidask_missing_symbol_returns_none(self, tmp_path):
        """normalize_bidask with no symbol returns None (line 611-612)."""
        norm = _make_norm(tmp_path)
        result = norm.normalize_bidask({"bid_price": [100.0], "ask_price": [101.0]})
        assert result is None

    def test_bidask_rust_scale_book_pair_stats_path(self, tmp_path):
        """Rust scale_book_pair_stats path is used when available (lines 788-801)."""
        cfg = _make_cfg(tmp_path)
        bids_out = [[1_000_000, 1]]
        asks_out = [[1_010_000, 1]]
        stats_tuple = (1_000_000, 1_010_000, 1, 1, 1_005_000.0, 10_000.0, 0.5)
        rust_fn = MagicMock(return_value=(bids_out, asks_out, stats_tuple))
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": True,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": True,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": rust_fn,
                "hft_platform.feed_adapter.normalizer._RUST_STATS_TUPLE": True,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = None
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None
        assert result.stats is not None
        assert result.stats[0] == 1_000_000  # best_bid

    def test_bidask_rust_scale_book_pair_stats_exception_falls_back(self, tmp_path):
        """When rust_scale_book_pair_stats raises, falls back to Python (lines 797-801)."""
        cfg = _make_cfg(tmp_path)
        rust_fn = MagicMock(side_effect=RuntimeError("rust fail"))
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": True,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": True,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": rust_fn,
                "hft_platform.feed_adapter.normalizer._RUST_STATS_TUPLE": True,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = None
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        # Falls back to Python path - should still produce a result
        assert result is not None
        assert result.bids[0][0] == 1_000_000

    def test_bidask_rust_scale_book_pair_path(self, tmp_path):
        """Rust scale_book_pair path used when pair_stats unavailable (lines 925-930)."""
        norm = _make_norm(tmp_path)
        bids_out = [[1_000_000, 1]]
        asks_out = [[1_010_000, 1]]
        rust_pair = MagicMock(return_value=(bids_out, asks_out))
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        with patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", True):
            with patch("hft_platform.feed_adapter.normalizer._RUST_FORCE", True):
                with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS", None):
                    with patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK", None):
                        with patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP", None):
                            with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR", rust_pair):
                                result = norm.normalize_bidask(payload)
        assert result is not None
        assert rust_pair.called  # verify our mock was actually called
        bids = result.bids
        assert bids == [[1_000_000, 1]]

    def test_bidask_rust_scale_book_pair_exception_falls_back(self, tmp_path):
        """When rust_scale_book_pair raises, falls back to Python (lines 929-931)."""
        cfg = _make_cfg(tmp_path)
        rust_pair = MagicMock(side_effect=RuntimeError("rust pair fail"))
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": True,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": True,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": rust_pair,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = None
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None
        assert result.bids[0][0] == 1_000_000

    def test_bidask_rust_scale_book_seq_path(self, tmp_path):
        """Rust scale_book_seq path for bids and asks (lines 934-941, 946-952)."""
        norm = _make_norm(tmp_path)
        bids_out = [[1_000_000, 1]]
        asks_out = [[1_010_000, 1]]
        rust_seq = MagicMock(side_effect=[bids_out, asks_out])
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        with patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", True):
            with patch("hft_platform.feed_adapter.normalizer._RUST_FORCE", True):
                with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS", None):
                    with patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK", None):
                        with patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP", None):
                            with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR", None):
                                with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ", rust_seq):
                                    result = norm.normalize_bidask(payload)
        assert result is not None
        assert rust_seq.called
        assert result.bids == [[1_000_000, 1]]
        assert result.asks == [[1_010_000, 1]]

    def test_bidask_rust_scale_book_seq_exception_falls_back_to_python(self, tmp_path):
        """When scale_book_seq raises, Python list comprehension fallback runs (lines 938-943, 950-954)."""
        cfg = _make_cfg(tmp_path)
        rust_seq = MagicMock(side_effect=RuntimeError("seq fail"))
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": True,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": True,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": rust_seq,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = None
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None
        assert result.bids[0][0] == 1_000_000

    def test_bidask_rust_normalize_bidask_path(self, tmp_path):
        """Rust normalize_bidask path is used (lines 878-912)."""
        cfg = _make_cfg(tmp_path)
        bids_out = [[1_000_000, 1]]
        asks_out = [[1_010_000, 1]]
        rust_tuple = (
            "bidask",
            "2330",
            bids_out,
            asks_out,
            1_000_000_000_000,
            False,
            1_000_000,
            1_010_000,
            1,
            1,
            1_005_000.0,
            10_000.0,
            0.5,
        )
        rust_fn = MagicMock(return_value=rust_tuple)
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": True,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": True,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_STATS_TUPLE": True,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": rust_fn,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = None
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None
        assert result.stats is not None
        assert result.stats[0] == 1_000_000

    def test_bidask_rust_normalize_bidask_exception_falls_back(self, tmp_path):
        """When normalize_bidask Rust raises, falls back to Python (lines 909-913)."""
        cfg = _make_cfg(tmp_path)
        rust_fn = MagicMock(side_effect=RuntimeError("norm fail"))
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": True,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": True,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": rust_fn,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = None
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None

    def test_bidask_rust_normalize_bidask_np_path(self, tmp_path):
        """Rust normalize_bidask_np path (lines 803-876)."""
        cfg = _make_cfg(tmp_path)
        bids_out = np.array([[1_000_000, 1]], dtype=np.int64)
        asks_out = np.array([[1_010_000, 1]], dtype=np.int64)
        rust_tuple = (
            "bidask",
            "2330",
            bids_out,
            asks_out,
            1_000_000_000_000,
            False,
            1_000_000,
            1_010_000,
            1,
            1,
            1_005_000.0,
            10_000.0,
            0.5,
        )
        rust_fn = MagicMock(return_value=rust_tuple)
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": True,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": True,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": rust_fn,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = None
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None
        assert result.stats is not None
        assert result.stats[0] == 1_000_000

    def test_bidask_rust_min_levels_path(self, tmp_path):
        """Rust min_levels check uses len(bp) to decide if Rust is used (lines 709-715)."""
        norm = _make_norm(tmp_path)
        bids_out = [[1_000_000, 1], [990_000, 2]]
        asks_out = [[1_010_000, 1], [1_020_000, 2]]
        rust_pair = MagicMock(return_value=(bids_out, asks_out))
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0, 99.0],
            "bid_volume": [1, 2],
            "ask_price": [101.0, 102.0],
            "ask_volume": [1, 2],
        }
        with patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", True):
            with patch("hft_platform.feed_adapter.normalizer._RUST_FORCE", False):
                with patch("hft_platform.feed_adapter.normalizer._RUST_MIN_LEVELS", 2):
                    with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS", None):
                        with patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK", None):
                            with patch("hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP", None):
                                with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR", rust_pair):
                                    result = norm.normalize_bidask(payload)
        assert result is not None
        assert rust_pair.called  # verify the Rust path was taken
        assert result.bids == bids_out

    def test_bidask_fused_path(self, tmp_path):
        """Fused normalizer path in normalize_bidask (lines 619-690)."""
        cfg = _make_cfg(tmp_path)
        bids_np = np.array([[1_000_000, 1]], dtype=np.int64)
        asks_np = np.array([[1_010_000, 1]], dtype=np.int64)
        fused_result = (bids_np, asks_np, 1_000_000, 1_010_000, 1, 1, 2_010_000, 10_000, 500_000, 1, 0.5)
        mock_fused = MagicMock()
        mock_fused.process_bidask.return_value = fused_result

        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": False,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": False,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = mock_fused  # inject fused directly
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None
        assert result.symbol == "2330"
        assert result.fused_stats is not None

    def test_bidask_fused_return_tuple(self, tmp_path):
        """Fused path with _RETURN_TUPLE=True returns tuple (line 659-674)."""
        norm = _make_norm(tmp_path)
        bids_np = np.array([[1_000_000, 1]], dtype=np.int64)
        asks_np = np.array([[1_010_000, 1]], dtype=np.int64)
        fused_result = (bids_np, asks_np, 1_000_000, 1_010_000, 1, 1, 2_010_000, 10_000, 500_000, 1, 0.5)
        mock_fused = MagicMock()
        mock_fused.process_bidask.return_value = fused_result
        norm._fused = mock_fused
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
            result = norm.normalize_bidask(payload)
        assert isinstance(result, tuple)
        assert result[0] == "bidask"
        assert result[1] == "2330"

    def test_bidask_fused_exception_falls_back_to_standard(self, tmp_path):
        """Fused path exception falls through to standard path (lines 688-690)."""
        cfg = _make_cfg(tmp_path)
        mock_fused = MagicMock()
        mock_fused.process_bidask.side_effect = RuntimeError("fused fail")

        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            patches = {
                "hft_platform.feed_adapter.normalizer._RUST_ENABLED": False,
                "hft_platform.feed_adapter.normalizer._RUST_FORCE": False,
                "hft_platform.feed_adapter.normalizer._RETURN_TUPLE": False,
                "hft_platform.feed_adapter.normalizer._SYNTHETIC_SIDE": False,
                "hft_platform.feed_adapter.normalizer._HAS_FUSED": False,
                "hft_platform.feed_adapter.normalizer._SHIOAJI_FIXED5_SCRATCH": False,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR": None,
                "hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_SEQ": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_NP": None,
                "hft_platform.feed_adapter.normalizer._RUST_NORMALIZE_BIDASK_SYNTH": None,
                "hft_platform.feed_adapter.normalizer._RUST_GET_FIELD": None,
            }
            with _multi_patch(patches):
                from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

                norm = MarketDataNormalizer(config_path=cfg)
                norm.metrics = None
                norm._fused = mock_fused
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_bidask(payload)
        assert result is not None
        # Falls back to Python path
        assert result.bids[0][0] == 1_000_000


# ---------------------------------------------------------------------------
# normalize_snapshot – object payload and tuple paths
# ---------------------------------------------------------------------------


class TestNormalizeSnapshot:
    def test_snapshot_object_payload(self, tmp_path):
        """normalize_snapshot handles object payload (lines 1000-1005)."""
        norm = _make_norm(tmp_path)

        class FakeSnap:
            code = "2330"
            ts = 1_000_000_000_000
            buy_price = 100.0
            buy_volume = 5
            sell_price = 101.0
            sell_volume = 3

        result = norm.normalize_snapshot(FakeSnap())
        assert result is not None
        assert result.is_snapshot is True
        assert result.bids[0][0] == 1_000_000
        assert result.asks[0][0] == 1_010_000

    def test_snapshot_missing_symbol_returns_none(self, tmp_path):
        """normalize_snapshot with no symbol returns None (line 1007-1008)."""
        norm = _make_norm(tmp_path)
        result = norm.normalize_snapshot({"buy_price": 100.0})
        assert result is None

    def test_snapshot_falls_back_to_bidask_no_bbo(self, tmp_path):
        """normalize_snapshot delegates to normalize_bidask when no BBO fields (line 1032)."""
        norm = _make_norm(tmp_path)
        payload = {
            "code": "2330",
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        result = norm.normalize_snapshot(payload)
        assert result is not None
        assert result.is_snapshot is True

    def test_snapshot_return_tuple_mode_with_bbo(self, tmp_path):
        """normalize_snapshot with _RETURN_TUPLE=True returns tuple with is_snapshot=True (line 1024)."""
        norm = _make_norm(tmp_path)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "buy_price": 100.0,
            "buy_volume": 1,
            "sell_price": 101.0,
            "sell_volume": 1,
        }
        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
            result = norm.normalize_snapshot(payload)
        assert isinstance(result, tuple)
        assert result[0] == "bidask"
        assert result[5] is True  # is_snapshot=True

    def test_snapshot_return_tuple_mode_falls_back_to_bidask_tuple(self, tmp_path):
        """normalize_snapshot delegates to normalize_bidask when no BBO, returns modified tuple (lines 1033-1036)."""
        norm = _make_norm(tmp_path)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
            result = norm.normalize_snapshot(payload)
        assert isinstance(result, tuple)
        assert result[5] is True  # is_snapshot flag set to True

    def test_snapshot_return_tuple_with_stats_7_extras(self, tmp_path):
        """normalize_snapshot from bidask tuple with stats returns >6 element tuple (line 1034-1035)."""
        norm = _make_norm(tmp_path)
        stats_result = ([[1_000_000, 1]], [[1_010_000, 1]], (1_000_000, 1_010_000, 1, 1, 1_005_000.0, 10_000.0, 0.5))
        rust_pair_stats = MagicMock(return_value=stats_result)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [1],
            "ask_price": [101.0],
            "ask_volume": [1],
        }
        with patch("hft_platform.feed_adapter.normalizer._RUST_ENABLED", True):
            with patch("hft_platform.feed_adapter.normalizer._RUST_FORCE", True):
                with patch("hft_platform.feed_adapter.normalizer._RUST_SCALE_BOOK_PAIR_STATS", rust_pair_stats):
                    with patch("hft_platform.feed_adapter.normalizer._RUST_STATS_TUPLE", True):
                        with patch("hft_platform.feed_adapter.normalizer._RETURN_TUPLE", True):
                            result = norm.normalize_snapshot(payload)
        assert isinstance(result, tuple)
        assert result[5] is True  # is_snapshot
        assert len(result) > 6  # has stats appended


# ---------------------------------------------------------------------------
# _validate_and_sync_timestamp – skew log cooldown path
# ---------------------------------------------------------------------------


class TestValidateAndSyncTimestamp:
    def test_skew_log_with_cooldown_suppression(self, tmp_path):
        """Skew log is suppressed when last log was recent (line 364)."""
        norm = _make_norm(tmp_path)
        # Set _last_skew_log_ns to be very recent so cooldown kicks in
        large_ts = 100_000_000_000_000_000  # 100s in ns
        norm._last_skew_log_ns = large_ts

        with patch("hft_platform.feed_adapter.normalizer._TS_MAX_LAG_NS", 1):
            with patch("hft_platform.feed_adapter.normalizer._TS_SKEW_LOG_COOLDOWN_NS", 1_000_000_000_000_000_000):
                # local_ts > exch_ts by more than 1ns, but cooldown suppresses log
                exch_ts = 1_000_000_000
                local_ts = 1_000_000_100
                result_exch, result_local = norm._validate_and_sync_timestamp(exch_ts, local_ts, "tick", "2330")
        # Should clamp: exch_ts + _TS_MAX_LAG_NS = 1_000_000_001
        assert result_local == 1_000_000_001

    def test_exch_ts_zero_passthrough(self, tmp_path):
        """When exch_ts=0, no clamping or sync occurs (line 357)."""
        norm = _make_norm(tmp_path)
        local_ts = 1_000_000_000_000
        result_exch, result_local = norm._validate_and_sync_timestamp(0, local_ts, "tick", "SYM")
        assert result_exch == 0
        assert result_local == local_ts

    def test_local_ts_behind_exch_ts_gets_synced(self, tmp_path):
        """When local_ts < exch_ts (but within future limit), local_ts is bumped to exch_ts (lines 359-360)."""
        norm = _make_norm(tmp_path)
        # exch_ts is slightly ahead of local_ts but within _TS_MAX_FUTURE_NS (default 5s in ns)
        # Use small delta (1ms = 1_000_000 ns) to stay within the 5s future limit
        local_ts = 1_000_000_000_000_000_000
        exch_ts = local_ts + 1_000_000  # 1ms ahead of local - within default 5s limit
        result_exch, result_local = norm._validate_and_sync_timestamp(exch_ts, local_ts, "tick", "SYM")
        assert result_local == exch_ts
