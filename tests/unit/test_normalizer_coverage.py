"""
Targeted coverage tests for feed_adapter/normalizer.py.

Focuses on branches not covered by existing tests:
- SymbolMetadata: reload_if_changed, symbols_for_tags, tick_size price_scale,
  contract_multiplier, product_type exchange-inference, order_params, _populate_registry
  option type handling
- MarketDataNormalizer: _maybe_synthesize_side, normalize_snapshot (object path),
  normalize_bidask _RETURN_TUPLE path, normalize_snapshot _RETURN_TUPLE path,
  _record_latency_metrics interarrival, _validate_and_sync_timestamp clamping
"""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock

import pytest

import hft_platform.feed_adapter.normalizer as norm_mod
from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer, SymbolMetadata

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def symbols_yaml(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(
        textwrap.dedent("""\
        symbols:
          - code: "2330"
            exchange: "TSE"
            price_scale: 10000
            tags: "equity|blue_chip"
            tax_rate_bps: 20
            commission_per_lot: 130000
            day_open: "09:00"
            day_close: "13:30"
          - code: "TMFD6"
            exchange: "TAIFEX"
            price_scale: 1
            tick_size: 1.0
            point_value: 10
            product_type: "future"
          - code: "TXO20000C202506"
            exchange: "OPT"
            product_type: "option"
            price_scale: 1
            tick_size: 1.0
            underlying: "TXFD6"
            strike: 20000
            right: "C"
            expiry: "2026-06-01"
          - code: "PUTTEST"
            exchange: "OPT"
            product_type: "option"
            price_scale: 1
            tick_size: 1.0
            underlying: "TXFD6"
            strike: 19000
            right: "P"
            expiry: "bad-date"
    """)
    )
    return str(cfg)


@pytest.fixture
def meta(symbols_yaml):
    return SymbolMetadata(symbols_yaml)


@pytest.fixture
def normalizer(symbols_yaml):
    return MarketDataNormalizer(symbols_yaml)


# ---------------------------------------------------------------------------
# SymbolMetadata — reload_if_changed
# ---------------------------------------------------------------------------


class TestSymbolMetadataReloadIfChanged:
    def test_reload_if_changed_returns_false_when_unchanged(self, meta):
        changed = meta.reload_if_changed()
        assert changed is False

    def test_reload_if_changed_returns_false_on_oserror(self, meta):
        meta.config_path = "/nonexistent/path/symbols.yaml"
        changed = meta.reload_if_changed()
        assert changed is False

    def test_reload_if_changed_detects_new_file(self, meta, tmp_path):
        new_cfg = tmp_path / "new_symbols.yaml"
        new_cfg.write_text("symbols:\n  - code: 'NEW'\n    exchange: 'TSE'\n")
        meta.config_path = str(new_cfg)
        meta._mtime = None  # Force detection
        changed = meta.reload_if_changed()
        assert changed is True


# ---------------------------------------------------------------------------
# SymbolMetadata — symbols_for_tags
# ---------------------------------------------------------------------------


class TestSymbolsForTags:
    def test_symbols_for_tags_found(self, meta):
        result = meta.symbols_for_tags(["equity"])
        assert "2330" in result

    def test_symbols_for_tags_multiple_tags(self, meta):
        result = meta.symbols_for_tags(["equity", "blue_chip"])
        assert "2330" in result

    def test_symbols_for_tags_empty_tag_ignored(self, meta):
        result = meta.symbols_for_tags(["", "equity"])
        assert "2330" in result

    def test_symbols_for_tags_unknown_tag_empty_set(self, meta):
        result = meta.symbols_for_tags(["nonexistent_tag"])
        assert len(result) == 0

    def test_symbols_for_tags_empty_iterable(self, meta):
        result = meta.symbols_for_tags([])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# SymbolMetadata — price_scale fallbacks
# ---------------------------------------------------------------------------


class TestPriceScale:
    def test_price_scale_from_tick_size(self, tmp_path):
        cfg = tmp_path / "tick.yaml"
        cfg.write_text("symbols:\n  - code: 'SYM'\n    exchange: 'TSE'\n    tick_size: 0.0001\n")
        m = SymbolMetadata(str(cfg))
        scale = m.price_scale("SYM")
        assert scale == 10000

    def test_price_scale_invalid_tick_size_falls_back_to_default(self, tmp_path):
        """Test that price_scale() falls back to default when tick_size conversion fails.

        We manually set the metadata entry to simulate a bad tick_size value
        without going through _populate_registry (which would also fail).
        """
        cfg = tmp_path / "sym.yaml"
        cfg.write_text("symbols:\n  - code: 'SYM'\n    exchange: 'TSE'\n    price_scale: 10000\n")
        m = SymbolMetadata(str(cfg))
        # Inject a bad tick_size directly into meta (simulating corrupted config)
        m.meta["SYM2"] = {"tick_size": "bad_value"}
        m._price_scale_cache.pop("SYM2", None)
        scale = m.price_scale("SYM2")
        assert scale == SymbolMetadata.DEFAULT_SCALE

    def test_price_scale_zero_tick_size_falls_back_to_default(self, tmp_path):
        cfg = tmp_path / "zero.yaml"
        cfg.write_text("symbols:\n  - code: 'SYM'\n    exchange: 'TSE'\n    tick_size: 0\n")
        m = SymbolMetadata(str(cfg))
        scale = m.price_scale("SYM")
        assert scale == SymbolMetadata.DEFAULT_SCALE

    def test_price_scale_unknown_symbol_returns_default(self, meta):
        assert meta.price_scale("UNKNOWN") == SymbolMetadata.DEFAULT_SCALE

    def test_price_scale_cached(self, meta):
        _ = meta.price_scale("2330")
        assert "2330" in meta._price_scale_cache


# ---------------------------------------------------------------------------
# SymbolMetadata — contract_multiplier
# ---------------------------------------------------------------------------


class TestContractMultiplier:
    def test_contract_multiplier_future(self, meta):
        assert meta.contract_multiplier("TMFD6") == 10

    def test_contract_multiplier_unknown_defaults_to_1(self, meta):
        assert meta.contract_multiplier("UNKNOWN") == 1

    def test_contract_multiplier_stock_defaults_to_1(self, meta):
        assert meta.contract_multiplier("2330") == 1


# ---------------------------------------------------------------------------
# SymbolMetadata — product_type exchange-based inference
# ---------------------------------------------------------------------------


class TestProductTypeExchangeInference:
    def test_tse_exchange_infers_stock(self, meta):
        assert meta.product_type("2330") == "stock"

    def test_taifex_exchange_infers_future(self, meta):
        assert meta.product_type("TMFD6") == "future"

    def test_opt_exchange_infers_option(self, meta):
        # PUTTEST has product_type already set to "option" directly
        assert meta.product_type("PUTTEST") == "option"

    def test_exchange_inference_idx(self, tmp_path):
        cfg = tmp_path / "idx.yaml"
        cfg.write_text("symbols:\n  - code: 'TAIEX'\n    exchange: 'IDX'\n")
        m = SymbolMetadata(str(cfg))
        assert m.product_type("TAIEX") == "index"

    def test_exchange_inference_futures_keyword(self, tmp_path):
        cfg = tmp_path / "fut.yaml"
        cfg.write_text("symbols:\n  - code: 'TXF'\n    exchange: 'FUTURES'\n")
        m = SymbolMetadata(str(cfg))
        assert m.product_type("TXF") == "future"

    def test_unknown_exchange_returns_empty(self, tmp_path):
        cfg = tmp_path / "unk.yaml"
        cfg.write_text("symbols:\n  - code: 'XX'\n    exchange: 'CRYPTO'\n")
        m = SymbolMetadata(str(cfg))
        assert m.product_type("XX") == ""

    def test_product_type_cached(self, meta):
        _ = meta.product_type("2330")
        assert "2330" in meta._product_type_cache


# ---------------------------------------------------------------------------
# SymbolMetadata — order_params
# ---------------------------------------------------------------------------


class TestOrderParams:
    def test_order_params_unknown_symbol(self, meta):
        params = meta.order_params("UNKNOWN")
        assert params == {}

    def test_order_params_extracts_known_keys(self, tmp_path):
        cfg = tmp_path / "order.yaml"
        cfg.write_text(
            textwrap.dedent("""\
            symbols:
              - code: "2330"
                exchange: "TSE"
                order_cond: "Cash"
                order_lot: "Common"
        """)
        )
        m = SymbolMetadata(str(cfg))
        params = m.order_params("2330")
        assert params.get("order_cond") == "Cash"
        assert params.get("order_lot") == "Common"


# ---------------------------------------------------------------------------
# SymbolMetadata — _populate_registry option type
# ---------------------------------------------------------------------------


class TestPopulateRegistryOption:
    def test_option_strike_and_call_right(self, meta):
        profile = meta.registry.get("TXO20000C202506")
        assert profile is not None
        from hft_platform.core.instrument_registry import InstrumentType, OptionRight

        assert profile.instrument_type == InstrumentType.OPTION
        assert profile.option_right == OptionRight.CALL
        assert profile.strike_scaled == 20000

    def test_option_put_right(self, meta):
        profile = meta.registry.get("PUTTEST")
        assert profile is not None
        from hft_platform.core.instrument_registry import OptionRight

        assert profile.option_right == OptionRight.PUT

    def test_option_expiry_bad_date_ignored(self, meta):
        profile = meta.registry.get("PUTTEST")
        assert profile is not None
        assert profile.expiry is None


# ---------------------------------------------------------------------------
# MarketDataNormalizer — _maybe_synthesize_side
# ---------------------------------------------------------------------------


class TestMaybeSynthesizeSide:
    @pytest.fixture
    def synth_normalizer(self, symbols_yaml, monkeypatch):
        monkeypatch.setattr(norm_mod, "_SYNTHETIC_SIDE", True)
        return MarketDataNormalizer(symbols_yaml)

    def test_bids_only_synthesizes_ask(self, synth_normalizer):
        bids = [[1000000, 10]]
        asks = []
        out_bids, out_asks, synthesized = synth_normalizer._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is True
        assert len(out_asks) == 1
        assert out_asks[0][0] > out_bids[0][0]

    def test_asks_only_synthesizes_bid(self, synth_normalizer):
        bids = []
        asks = [[1001000, 8]]
        out_bids, out_asks, synthesized = synth_normalizer._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is True
        assert len(out_bids) == 1
        assert out_bids[0][0] < out_asks[0][0]

    def test_both_sides_no_synthesis(self, synth_normalizer):
        bids = [[1000000, 10]]
        asks = [[1001000, 8]]
        out_bids, out_asks, synthesized = synth_normalizer._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is False
        assert out_bids is bids
        assert out_asks is asks

    def test_synthesis_disabled_no_change(self, symbols_yaml, monkeypatch):
        monkeypatch.setattr(norm_mod, "_SYNTHETIC_SIDE", False)
        n = MarketDataNormalizer(symbols_yaml)
        bids = []
        asks = [[1001000, 8]]
        out_bids, out_asks, synthesized = n._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is False
        assert out_bids is bids

    def test_normalize_bidask_triggers_synthesis(self, synth_normalizer, monkeypatch):
        """normalize_bidask with bids-only should produce synthesized asks via event."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        payload = {
            "code": "2330",
            "ts": 1_000_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [],
            "ask_volume": [],
        }
        event = synth_normalizer.normalize_bidask(payload)
        assert isinstance(event, BidAskEvent)
        # stats is None when synthesized
        assert event.stats is None

    def test_synthesis_with_tick_size_from_symbol(self, synth_normalizer):
        """TMFD6 has tick_size=1.0; synthesize side using configured tick."""
        bids = []
        asks = [[20000, 5]]
        out_bids, out_asks, synthesized = synth_normalizer._maybe_synthesize_side("TMFD6", bids, asks, 1)
        assert synthesized is True
        assert out_bids[0][0] == 19999  # best_ask - 1 tick

    def test_synthesis_numpy_asks(self, synth_normalizer):
        import numpy as np

        bids = []
        asks = np.array([[1001000, 8]], dtype=np.int64)
        out_bids, out_asks, synthesized = synth_normalizer._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is True


# ---------------------------------------------------------------------------
# MarketDataNormalizer — normalize_tick attribute-based payload
# ---------------------------------------------------------------------------


class TestNormalizeTickObjectPayload:
    def test_normalize_tick_object_payload(self, normalizer):
        payload = MagicMock()
        payload.code = "2330"
        payload.Code = None
        payload.ts = 1_620_000_000_000_000
        payload.datetime = None
        payload.close = 150.0
        payload.Close = None
        payload.volume = 10
        payload.Volume = None
        payload.total_volume = 1000
        payload.simtrade = 0
        payload.intraday_odd = 0
        event = normalizer.normalize_tick(payload)
        assert isinstance(event, TickEvent)
        assert event.price == 1_500_000  # 150.0 * 10000
        assert event.volume == 10

    def test_normalize_tick_object_no_symbol(self, normalizer):
        payload = MagicMock()
        payload.code = None
        payload.Code = None
        payload.ts = None
        payload.datetime = None
        payload.close = 100.0
        payload.Close = None
        payload.volume = 5
        payload.Volume = None
        payload.total_volume = 0
        payload.simtrade = 0
        payload.intraday_odd = 0
        result = normalizer.normalize_tick(payload)
        assert result is None


# ---------------------------------------------------------------------------
# MarketDataNormalizer — normalize_bidask _RETURN_TUPLE
# ---------------------------------------------------------------------------


class TestNormalizeBidAskReturnTuple:
    def test_return_tuple_with_stats(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
        monkeypatch.setattr(norm_mod, "_RUST_ENABLED", False)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_PAIR_STATS", None)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_BIDASK", None)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_BIDASK_NP", None)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_PAIR", None)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_SEQ", None)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "bid_price": [100.0, 99.5],
            "bid_volume": [10, 5],
            "ask_price": [100.5, 101.0],
            "ask_volume": [8, 3],
        }
        result = normalizer.normalize_bidask(payload)
        assert isinstance(result, tuple)
        assert result[0] == "bidask"
        assert result[1] == "2330"

    def test_return_tuple_without_stats(self, normalizer, monkeypatch):
        """Pure Python path with no Rust — returns short 6-element tuple."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
        monkeypatch.setattr(norm_mod, "_RUST_ENABLED", False)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_PAIR_STATS", None)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_BIDASK", None)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_BIDASK_NP", None)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_PAIR", None)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_SEQ", None)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "bid_price": [],
            "bid_volume": [],
            "ask_price": [],
            "ask_volume": [],
        }
        result = normalizer.normalize_bidask(payload)
        assert isinstance(result, tuple)
        assert result[0] == "bidask"

    def test_return_tuple_no_symbol_returns_none(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
        result = normalizer.normalize_bidask({"code": None})
        assert result is None


# ---------------------------------------------------------------------------
# MarketDataNormalizer — normalize_snapshot
# ---------------------------------------------------------------------------


class TestNormalizeSnapshot:
    def test_snapshot_dict_with_buy_sell(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "buy_price": 100.0,
            "buy_volume": 10,
            "sell_price": 100.5,
            "sell_volume": 8,
        }
        event = normalizer.normalize_snapshot(payload)
        assert isinstance(event, BidAskEvent)
        assert event.is_snapshot is True

    def test_snapshot_dict_return_tuple(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "buy_price": 100.0,
            "buy_volume": 10,
            "sell_price": 100.5,
            "sell_volume": 8,
        }
        result = normalizer.normalize_snapshot(payload)
        assert isinstance(result, tuple)
        assert result[5] is True  # is_snapshot flag

    def test_snapshot_no_symbol_returns_none(self, normalizer):
        result = normalizer.normalize_snapshot({"buy_price": 100.0})
        assert result is None

    def test_snapshot_object_payload(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        payload = MagicMock()
        payload.code = "2330"
        payload.Code = None
        payload.ts = 1_620_000_000_000_000
        payload.datetime = None
        payload.buy_price = 100.0
        payload.buy_volume = 10
        payload.sell_price = 100.5
        payload.sell_volume = 8
        event = normalizer.normalize_snapshot(payload)
        assert isinstance(event, BidAskEvent)
        assert event.is_snapshot is True

    def test_snapshot_falls_back_to_normalize_bidask(self, normalizer, monkeypatch):
        """When no buy_price/sell_price, falls back to normalize_bidask."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [100.5],
            "ask_volume": [8],
        }
        event = normalizer.normalize_snapshot(payload)
        assert isinstance(event, BidAskEvent)
        assert event.is_snapshot is True

    def test_snapshot_fallback_return_tuple_long(self, normalizer, monkeypatch):
        """Fallback path returns 13-element tuple with is_snapshot=True."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
        monkeypatch.setattr(norm_mod, "_RUST_ENABLED", False)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_PAIR_STATS", None)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_BIDASK", None)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_BIDASK_NP", None)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_PAIR", None)
        monkeypatch.setattr(norm_mod, "_RUST_SCALE_BOOK_SEQ", None)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [100.5],
            "ask_volume": [8],
        }
        result = normalizer.normalize_snapshot(payload)
        assert isinstance(result, tuple)
        assert result[5] is True  # is_snapshot position

    def test_snapshot_buy_only_no_sell(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "buy_price": 100.0,
            "buy_volume": 10,
        }
        event = normalizer.normalize_snapshot(payload)
        # buy_price is set so it enters the bids/asks branch
        assert event is not None


# ---------------------------------------------------------------------------
# MarketDataNormalizer — _record_latency_metrics interarrival
# ---------------------------------------------------------------------------


class TestRecordLatencyMetrics:
    def test_interarrival_metric_on_second_call(self, normalizer):
        """Call normalize_tick twice; second call exercises interarrival branch."""
        payload = {
            "code": "2330",
            "close": 100.0,
            "volume": 5,
            "ts": 1_620_000_000_000_000,
        }
        # First call sets _last_local_ts_tick
        normalizer.normalize_tick(payload)
        first_ts = normalizer._last_local_ts_tick
        assert first_ts > 0
        # Second call exercises `if last:` interarrival branch
        normalizer.normalize_tick(payload)
        second_ts = normalizer._last_local_ts_tick
        assert second_ts >= first_ts

    def test_interarrival_bidask_on_second_call(self, normalizer):
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [100.5],
            "ask_volume": [8],
        }
        normalizer.normalize_bidask(payload)
        normalizer.normalize_bidask(payload)
        assert normalizer._last_local_ts_bidask > 0


# ---------------------------------------------------------------------------
# MarketDataNormalizer — _validate_and_sync_timestamp
# ---------------------------------------------------------------------------


class TestValidateAndSyncTimestamp:
    def test_future_ts_clamped_to_now(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_TS_MAX_FUTURE_NS", 1_000_000_000)  # 1 second
        now_ns = 1_000_000_000_000_000_000
        exch_ts = now_ns + 5_000_000_000  # 5 seconds in the future
        out_exch, out_local = normalizer._validate_and_sync_timestamp(exch_ts, now_ns, "tick", "2330")
        assert out_exch == now_ns  # Clamped to now

    def test_local_ts_synced_up_to_exch_ts(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_TS_MAX_FUTURE_NS", 0)  # No future clamping
        now_ns = 1_000_000_000_000_000_000
        exch_ts = now_ns + 100  # Slightly in the future (within tolerance)
        out_exch, out_local = normalizer._validate_and_sync_timestamp(exch_ts, now_ns - 100, "tick", "2330")
        # local_ts < exch_ts → local_ts synced to exch_ts
        assert out_local == exch_ts

    def test_zero_exch_ts_passes_through(self, normalizer):
        out_exch, out_local = normalizer._validate_and_sync_timestamp(0, 1_000_000, "tick", "2330")
        assert out_exch == 0

    def test_excessive_lag_clamped(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_TS_MAX_LAG_NS", 1_000_000_000)  # 1 second max lag
        now_ns = 1_000_000_000_000_000_000
        exch_ts = now_ns - 10_000_000_000  # 10 seconds in the past
        out_exch, out_local = normalizer._validate_and_sync_timestamp(exch_ts, now_ns, "tick", "2330")
        # local_ts is clamped to exch_ts + max_lag
        assert out_local == exch_ts + 1_000_000_000


# ---------------------------------------------------------------------------
# MarketDataNormalizer — normalize_tick _RETURN_TUPLE path (Python fallback)
# ---------------------------------------------------------------------------


class TestNormalizeTickReturnTuple:
    def test_normalize_tick_return_tuple_python_path(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_TICK", None)
        monkeypatch.setattr(norm_mod, "_RUST_ENABLED", False)
        payload = {
            "code": "2330",
            "close": 100.0,
            "volume": 5,
            "ts": 1_620_000_000_000_000,
        }
        result = normalizer.normalize_tick(payload)
        assert isinstance(result, tuple)
        assert result[0] == "tick"
        assert result[1] == "2330"
        assert result[2] == 1_000_000  # 100.0 * 10000
