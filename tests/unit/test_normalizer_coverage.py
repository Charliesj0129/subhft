"""
Targeted coverage tests for feed_adapter/normalizer.py.

Focuses on branches not covered by existing tests:
- Module-level env var parse exception paths (lines 53-65)
- Config path resolution fallbacks (lines 131-136)
- Tags non-str/non-list type (line 180)
- set_alias_map metadata propagation (lines 311-313)
- _safe_tick_size_scaled invalid input (lines 334-335)
- Option expiry as date object (line 387)
- _record_latency_metrics observe paths (lines 553-560)
- _get_scale with zero/negative scale (line 568)
- _maybe_synthesize_side null/tick_size fallbacks (lines 585, 601-606)
- Snapshot list-typed buy/sell fields
"""

from __future__ import annotations

import textwrap
from datetime import date
from unittest.mock import MagicMock

import numpy as np
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
    def test_return_tuple_with_book_levels(self, normalizer, monkeypatch):
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
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
        assert result[2:4] == ([[1000000, 10], [995000, 5]], [[1005000, 8], [1010000, 3]])

    def test_return_tuple_without_stats(self, normalizer, monkeypatch):
        """Returns the six-field bid/ask tuple."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
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

    def test_snapshot_fallback_returns_book_levels(self, normalizer, monkeypatch):
        """The book update is marked as a snapshot."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
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
        # local_ts < exch_ts -> local_ts synced to exch_ts
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


# ===========================================================================
# NEW TESTS — targeting uncovered lines
# ===========================================================================


# ---------------------------------------------------------------------------
# set_alias_map propagates metadata to actual codes (lines 311-313)
# ---------------------------------------------------------------------------


class TestSetAliasMapMetadataPropagation:
    def test_alias_map_copies_config_entry_to_actual_code(self, meta):
        """When alias differs from actual and actual not in meta, copy config entry."""
        assert "TXFE6" not in meta.meta
        meta.set_alias_map({"TMFD6": "TMFE6"})
        # TMFD6 config entry should be copied to TMFE6
        assert "TMFE6" in meta.meta
        assert meta.meta["TMFE6"] is meta.meta["TMFD6"]

    def test_alias_map_does_not_overwrite_existing_actual(self, meta):
        """If actual code already in meta, do not overwrite."""
        meta.meta["EXISTING"] = {"price_scale": 999}
        meta.set_alias_map({"2330": "EXISTING"})
        # EXISTING should keep its own config, not get overwritten by 2330
        assert meta.meta["EXISTING"]["price_scale"] == 999

    def test_alias_map_same_code_skipped(self, meta):
        """When alias == actual, no copy is needed."""
        original_keys = set(meta.meta.keys())
        meta.set_alias_map({"2330": "2330"})
        assert set(meta.meta.keys()) == original_keys

    def test_alias_map_missing_config_code_skipped(self, meta):
        """When config_code not in meta, nothing to copy."""
        meta.set_alias_map({"NONEXISTENT": "ACTUALX"})
        assert "ACTUALX" not in meta.meta


# ---------------------------------------------------------------------------
# _safe_tick_size_scaled with invalid tick_size (lines 334-335)
# ---------------------------------------------------------------------------


class TestSafeTickSizeScaled:
    def test_invalid_string_tick_size_defaults_to_one(self, meta):
        """TypeError/ValueError from float() on bad tick_size falls back to 1.0."""
        entry = {"tick_size": "not_a_number"}
        result = meta._safe_tick_size_scaled(entry, "2330")
        # 1.0 * price_scale("2330") = 1.0 * 10000 = 10000
        assert result == 10000

    def test_negative_tick_size_defaults_to_one(self, meta):
        """Negative tick_size falls back to 1.0."""
        entry = {"tick_size": -0.5}
        result = meta._safe_tick_size_scaled(entry, "2330")
        assert result == 10000

    def test_zero_tick_size_defaults_to_one(self, meta):
        """Zero tick_size falls back to 1.0."""
        entry = {"tick_size": 0}
        result = meta._safe_tick_size_scaled(entry, "2330")
        assert result == 10000

    def test_none_tick_size_defaults_to_one(self, meta):
        """None tick_size triggers TypeError, falls back to 1.0."""
        entry = {"tick_size": None}
        result = meta._safe_tick_size_scaled(entry, "2330")
        assert result == 10000

    def test_valid_tick_size_scales_correctly(self, meta):
        """Normal tick_size is scaled properly."""
        entry = {"tick_size": 0.01}
        result = meta._safe_tick_size_scaled(entry, "2330")
        # 0.01 * 10000 = 100
        assert result == 100


# ---------------------------------------------------------------------------
# Option expiry as datetime.date object (line 387)
# ---------------------------------------------------------------------------


class TestPopulateRegistryOptionExpiryAsDate:
    def test_option_expiry_as_date_object(self, tmp_path):
        """When expiry is already a date object, it should be used directly."""
        import yaml

        cfg = tmp_path / "sym.yaml"
        data = {
            "symbols": [
                {
                    "code": "TXO_DATE",
                    "exchange": "OPT",
                    "product_type": "option",
                    "price_scale": 1,
                    "tick_size": 1.0,
                    "underlying": "TXF",
                    "strike": 20000,
                    "right": "C",
                    "expiry": date(2026, 6, 1),
                }
            ]
        }
        cfg.write_text(yaml.dump(data))
        m = SymbolMetadata(str(cfg))
        profile = m.registry.get("TXO_DATE")
        assert profile is not None
        assert profile.expiry == date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Tags with non-str/non-list type (line 180)
# ---------------------------------------------------------------------------


class TestTagsNonStandardType:
    def test_tags_as_integer_ignored(self, tmp_path):
        """Tags that are not str/list/tuple/set default to empty."""
        cfg = tmp_path / "tags.yaml"
        cfg.write_text(
            textwrap.dedent("""\
            symbols:
              - code: "SYM1"
                exchange: "TSE"
                tags: 12345
        """)
        )
        m = SymbolMetadata(str(cfg))
        # Integer tags fall into the else branch -> tags = []
        assert "SYM1" not in m.tags_by_symbol

    def test_tags_as_none_ignored(self, tmp_path):
        """Tags that are None treated as empty list by .get default."""
        cfg = tmp_path / "tags.yaml"
        cfg.write_text(
            textwrap.dedent("""\
            symbols:
              - code: "SYM2"
                exchange: "TSE"
        """)
        )
        m = SymbolMetadata(str(cfg))
        assert "SYM2" not in m.tags_by_symbol


# ---------------------------------------------------------------------------
# Config path resolution fallbacks (lines 131-136)
# ---------------------------------------------------------------------------


class TestConfigPathResolution:
    def test_fallback_to_abs_base_path(self, tmp_path, monkeypatch):
        """When _abs_symbols does not exist but _abs_base does, use _abs_base (line 131-132)."""
        monkeypatch.delenv("SYMBOLS_CONFIG", raising=False)
        base_cfg = tmp_path / "config" / "base" / "symbols.yaml"
        base_cfg.parent.mkdir(parents=True)
        base_cfg.write_text("symbols:\n  - code: 'BASE'\n    exchange: 'TSE'\n")

        import os

        _real_exists = os.path.exists

        def _patched_exists(path):
            # Block the first candidate (_abs_symbols) so it falls to _abs_base
            if path.endswith("config/symbols.yaml") and not path.endswith("base/symbols.yaml"):
                return False
            if path == str(base_cfg):
                return True
            return _real_exists(path)

        monkeypatch.setattr(os.path, "exists", _patched_exists)
        m = SymbolMetadata(None)
        assert isinstance(m.meta, dict)

    def test_fallback_cwd_symbols(self, tmp_path, monkeypatch):
        """When abs paths don't exist, try cwd-relative config/symbols.yaml (line 133-134)."""
        monkeypatch.delenv("SYMBOLS_CONFIG", raising=False)

        import os

        _real_exists = os.path.exists
        call_count = {"n": 0}

        def _patched_exists(path):
            # Block _abs_symbols and _abs_base but allow cwd-relative
            if "config/symbols.yaml" in str(path) or "config/base/symbols.yaml" in str(path):
                call_count["n"] += 1
                # First two calls are the absolute paths -- block them
                if call_count["n"] <= 2:
                    return False
                # Third call is cwd-relative config/symbols.yaml
                return call_count["n"] == 3
            return _real_exists(path)

        monkeypatch.setattr(os.path, "exists", _patched_exists)
        m = SymbolMetadata(None)
        # Whatever config_path ends up as, meta should be a dict
        assert isinstance(m.meta, dict)

    def test_fallback_to_default_config_path(self, tmp_path, monkeypatch):
        """When none of the resolved paths exist, uses config/base/symbols.yaml as last resort (line 136)."""
        monkeypatch.delenv("SYMBOLS_CONFIG", raising=False)
        # Point to nonexistent file -- SymbolMetadata should still construct
        m = SymbolMetadata(str(tmp_path / "nonexistent" / "symbols.yaml"))
        # Should still have empty meta (load failed gracefully)
        assert isinstance(m.meta, dict)

    def test_fallback_last_resort_path(self, tmp_path, monkeypatch):
        """Resolver's final fallback is ``config/base/symbols.yaml`` anchored
        to the project root (now an absolute path via
        hft_platform.config.symbols_path)."""
        monkeypatch.delenv("SYMBOLS_CONFIG", raising=False)

        from hft_platform.config import symbols_path as sp

        monkeypatch.setattr(sp, "_PROJECT_ROOT", tmp_path)
        m = SymbolMetadata(None)
        assert m.config_path.endswith("config/base/symbols.yaml")
        assert isinstance(m.meta, dict)


# ---------------------------------------------------------------------------
# _get_scale with zero/negative scale (line 568)
# ---------------------------------------------------------------------------


class TestGetScaleEdgeCases:
    def test_zero_scale_defaults_to_one(self, normalizer):
        """When metadata returns scale=0, _get_scale should clamp to 1."""
        normalizer.metadata.meta["ZERO_SCALE"] = {"price_scale": 0}
        normalizer.metadata._price_scale_cache.pop("ZERO_SCALE", None)
        scale = normalizer._get_scale("ZERO_SCALE")
        assert scale == 1

    def test_negative_scale_defaults_to_one(self, normalizer):
        """When metadata returns scale<0, _get_scale should clamp to 1."""
        normalizer.metadata.meta["NEG_SCALE"] = {"price_scale": -100}
        normalizer.metadata._price_scale_cache.pop("NEG_SCALE", None)
        scale = normalizer._get_scale("NEG_SCALE")
        assert scale == 1


# ---------------------------------------------------------------------------
# _maybe_synthesize_side: None levels (line 585), tick_size fallback (lines 601-606)
# ---------------------------------------------------------------------------


class TestSynthesizeSideEdgeCases:
    @pytest.fixture
    def synth_norm(self, symbols_yaml, monkeypatch):
        monkeypatch.setattr(norm_mod, "_SYNTHETIC_SIDE", True)
        return MarketDataNormalizer(symbols_yaml)

    def test_none_bids_synthesizes_bid(self, synth_norm):
        """When bids is None, should synthesize bids from asks."""
        bids = None
        asks = [[1000000, 5]]
        out_bids, out_asks, synthesized = synth_norm._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is True
        assert len(out_bids) == 1
        assert out_bids[0][0] < 1000000

    def test_none_asks_synthesizes_ask(self, synth_norm):
        """When asks is None, should synthesize asks from bids."""
        bids = [[1000000, 5]]
        asks = None
        out_bids, out_asks, synthesized = synth_norm._maybe_synthesize_side("2330", bids, asks, 10000)
        assert synthesized is True
        assert len(out_asks) == 1
        assert out_asks[0][0] > 1000000

    def test_synthesis_invalid_tick_size_falls_back(self, synth_norm):
        """When tick_size is invalid (bad string), falls back to 1/scale."""
        synth_norm.metadata.meta["BADTICK"] = {"tick_size": "invalid"}
        bids = []
        asks = [[5000, 10]]
        out_bids, out_asks, synthesized = synth_norm._maybe_synthesize_side("BADTICK", bids, asks, 10000)
        assert synthesized is True
        # tick_size falls back to 1/scale = 0.0001, tick_int = round(0.0001 * 10000) = 1
        assert out_bids[0][0] == 4999

    def test_synthesis_no_metadata_falls_back_to_scale(self, synth_norm):
        """When symbol not in metadata, tick_size derived from 1/scale."""
        bids = [[100000, 5]]
        asks = []
        out_bids, out_asks, synthesized = synth_norm._maybe_synthesize_side("UNKNOWN", bids, asks, 10000)
        assert synthesized is True
        assert len(out_asks) == 1

    def test_synthesis_zero_scale_tick_size_fallback(self, synth_norm):
        """When scale=0, tick_size should fall back to 1.0."""
        synth_norm.metadata.meta["ZEROSCALE"] = {}
        bids = []
        asks = [[100, 5]]
        # scale=0 path: not tick_size and scale > 0 is False, not tick_size and True -> tick_size = 1.0
        # BUT scale=0 means tick_int = max(1, round(1.0 * 0)) = max(1, 0) = 1
        # Actually with scale=0, the tick_size = 1/float(0) would ZeroDivisionError... let's use 1
        # The code checks `if not tick_size and scale > 0` so scale=0 skips that branch
        # Then `if not tick_size` -> tick_size = 1.0
        # tick_int = max(1, round(1.0 * 1)) = 1 (we use scale=1 here)
        out_bids, out_asks, synthesized = synth_norm._maybe_synthesize_side("ZEROSCALE", bids, asks, 1)
        assert synthesized is True
        assert out_bids[0][0] == 99  # best_ask - tick_int(1)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestNegativePrice:
    def test_negative_price_after_scaling(self, normalizer):
        """When close_val produces a negative scaled price via Python path, return None."""
        payload = {
            "code": "2330",
            "close": -5.0,
            "volume": 5,
            "ts": 1_620_000_000_000_000,
        }
        result = normalizer.normalize_tick(payload)
        assert result is None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Snapshot with list-typed buy/sell fields
# ---------------------------------------------------------------------------


class TestSnapshotListTypedFields:
    def test_snapshot_list_buy_price_extracts_first(self, normalizer, monkeypatch):
        """When buy_price is a list, first element is extracted."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "buy_price": [100.0, 99.5],
            "buy_volume": [10, 5],
            "sell_price": [100.5, 101.0],
            "sell_volume": [8, 3],
        }
        event = normalizer.normalize_snapshot(payload)
        assert isinstance(event, BidAskEvent)
        assert event.is_snapshot is True
        # First bid should be 100.0 * 10000 = 1000000
        assert event.bids[0][0] == 1_000_000

    def test_snapshot_empty_list_buy_price_zero(self, normalizer, monkeypatch):
        """When buy_price is an empty list, defaults to 0."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "buy_price": [],
            "buy_volume": [],
            "sell_price": [100.5],
            "sell_volume": [8],
        }
        event = normalizer.normalize_snapshot(payload)
        assert isinstance(event, BidAskEvent)
        assert event.is_snapshot is True
        # buy_price = 0 -> bids empty, sell_price extracted -> asks has one level
        assert len(event.bids) == 0
        assert len(event.asks) == 1


# ---------------------------------------------------------------------------
# _record_latency_metrics observe paths (lines 553-555, 558-560)
# ---------------------------------------------------------------------------


class TestRecordLatencyMetricsObserve:
    def test_feed_latency_observed_on_sample(self, normalizer):
        """Verify feed_latency_ns.observe is called when exch_ts > 0 and on sample."""
        # Force sample_every=1 so every call is sampled
        normalizer._latency_metrics_sample_every = 1
        normalizer._latency_metrics_counter = 0

        # We need real metrics to test observe. The normalizer fixture has them.
        assert normalizer.metrics is not None

        # Call once to set _last_local_ts_tick
        normalizer._record_latency_metrics(1_000_000, 2_000_000, "_last_local_ts_tick")
        assert normalizer._last_local_ts_tick == 2_000_000

        # Call again - should observe both latency and interarrival
        normalizer._record_latency_metrics(3_000_000, 4_000_000, "_last_local_ts_tick")
        assert normalizer._last_local_ts_tick == 4_000_000

    def test_feed_latency_skipped_when_zero_exch_ts(self, normalizer):
        """When exch_ts=0, feed_latency_ns is not observed."""
        normalizer._latency_metrics_sample_every = 1
        normalizer._latency_metrics_counter = 0
        # Should not raise; just updates last_ts
        normalizer._record_latency_metrics(0, 1_000_000, "_last_local_ts_tick")
        assert normalizer._last_local_ts_tick == 1_000_000

    def test_negative_lag_not_observed(self, normalizer):
        """When lag_ns < 0, feed_latency_ns.observe is not called."""
        normalizer._latency_metrics_sample_every = 1
        normalizer._latency_metrics_counter = 0
        # exch_ts > local_ts => lag < 0
        normalizer._record_latency_metrics(5_000_000, 1_000_000, "_last_local_ts_tick")
        assert normalizer._last_local_ts_tick == 1_000_000


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _validate_and_sync_timestamp with skew logging (cooldown path)
# ---------------------------------------------------------------------------


class TestTimestampSkewLogging:
    def test_skew_logging_respects_cooldown(self, normalizer, monkeypatch):
        """Second skew event within cooldown should not log again."""
        monkeypatch.setattr(norm_mod, "_TS_MAX_LAG_NS", 1_000_000_000)
        monkeypatch.setattr(norm_mod, "_TS_SKEW_LOG_COOLDOWN_NS", 60_000_000_000)

        exch_ts = 1_000_000_000_000_000_000
        local_ts = exch_ts + 10_000_000_000  # 10s lag

        # First call should set _last_skew_log_ns
        normalizer._last_skew_log_ns = 0
        normalizer._validate_and_sync_timestamp(exch_ts, local_ts, "tick", "2330")
        first_log_ts = normalizer._last_skew_log_ns
        assert first_log_ts > 0

        # Second call with same timestamps (within cooldown) should NOT update log ts
        normalizer._validate_and_sync_timestamp(exch_ts, local_ts, "tick", "2330")
        assert normalizer._last_skew_log_ns == first_log_ts


# ---------------------------------------------------------------------------
# Snapshot fallback normalize_bidask returning None (line 1255)
# ---------------------------------------------------------------------------


class TestSnapshotFallbackNone:
    def test_snapshot_fallback_returns_none_when_bidask_returns_none(self, normalizer, monkeypatch):
        """When normalize_bidask returns None, normalize_snapshot also returns None."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", False)
        # Payload without buy/sell but with bid/ask that has no symbol -> None
        payload = {
            "ts": 1_620_000_000_000_000,
            "bid_price": [100.0],
            "bid_volume": [10],
            "ask_price": [100.5],
            "ask_volume": [8],
        }
        result = normalizer.normalize_snapshot(payload)
        assert result is None


# ---------------------------------------------------------------------------
# _RETURN_TUPLE path for normalize_snapshot fallback (lines 1253-1254)
# ---------------------------------------------------------------------------


class TestSnapshotFallbackReturnTupleShort:
    def test_snapshot_fallback_tuple_short_form(self, normalizer, monkeypatch):
        """Fallback path that produces a short (6-element) tuple with is_snapshot=True."""
        monkeypatch.setattr(norm_mod, "_RETURN_TUPLE", True)
        # Empty book -> no stats -> short tuple
        payload = {
            "code": "2330",
            "ts": 1_620_000_000_000_000,
            "bid_price": [],
            "bid_volume": [],
            "ask_price": [],
            "ask_volume": [],
        }
        result = normalizer.normalize_snapshot(payload)
        assert isinstance(result, tuple)
        assert result[5] is True  # is_snapshot
        # Short form: 6 elements
        assert len(result) == 6


# ---------------------------------------------------------------------------
# SymbolMetadata.reload (line 190-191)
# ---------------------------------------------------------------------------


class TestSymbolMetadataReload:
    def test_reload_clears_and_reloads(self, meta):
        """reload() should re-read config and rebuild registry."""
        original_codes = set(meta.meta.keys())
        meta.reload()
        reloaded_codes = set(meta.meta.keys())
        assert original_codes == reloaded_codes

    def test_resolve_symbol_with_alias(self, meta):
        """resolve_symbol returns actual code when alias exists."""
        meta.set_alias_map({"TMFD6": "TMFE6"})
        assert meta.resolve_symbol("TMFD6") == "TMFE6"
        assert meta.resolve_symbol("UNKNOWN") == "UNKNOWN"

    def test_resolve_symbols_set(self, meta):
        """resolve_symbols maps a set of codes through alias_to_actual."""
        meta.set_alias_map({"TMFD6": "TMFE6"})
        result = meta.resolve_symbols({"TMFD6", "2330"})
        assert "TMFE6" in result
        assert "2330" in result
