"""Verify SymbolMetadata backward-compat wrapper covers full public API."""

from __future__ import annotations

import pytest
import yaml

from hft_platform.feed_adapter.normalizer import SymbolMetadata


@pytest.fixture
def symbols_yaml(tmp_path):
    data = {
        "symbols": [
            {
                "code": "TXFC0",
                "exchange": "FUT",
                "tags": ["futures", "front_month", "txf"],
                "point_value": 200,
                "tick_size": 1.0,
            },
            {"code": "2330", "exchange": "TSE", "tags": ["stocks", "tw50"], "tick_size": 0.5},
            {
                "code": "TXO22000C202604",
                "exchange": "OPT",
                "tags": ["options", "txo"],
                "product_type": "option",
                "point_value": 50,
            },
        ],
    }
    path = tmp_path / "symbols.yaml"
    path.write_text(yaml.dump(data))
    return str(path)


class TestSymbolMetadataCompat:
    def test_price_scale(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert isinstance(meta.price_scale("TXFC0"), int)

    def test_contract_multiplier(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert meta.contract_multiplier("TXFC0") == 200
        assert meta.contract_multiplier("2330") == 1

    def test_exchange(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert meta.exchange("TXFC0") == "FUT"
        assert meta.exchange("2330") == "TSE"
        assert meta.exchange("UNKNOWN") == ""

    def test_product_type(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert meta.product_type("TXFC0") == "future"
        assert meta.product_type("2330") == "stock"
        assert meta.product_type("TXO22000C202604") == "option"

    def test_order_params(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        params = meta.order_params("TXFC0")
        assert isinstance(params, dict)

    def test_symbols_for_tags(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        result = meta.symbols_for_tags(["futures"])
        assert "TXFC0" in result

    def test_reload(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        meta.reload()
        assert "TXFC0" in meta.meta

    def test_reload_if_changed(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        changed = meta.reload_if_changed()
        assert isinstance(changed, bool)

    def test_meta_attribute(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert isinstance(meta.meta, dict)
        assert "TXFC0" in meta.meta

    def test_symbols_by_tag_attribute(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert isinstance(meta.symbols_by_tag, dict)
        meta.symbols_by_tag["test_tag"] = {"SYM1"}
        assert "SYM1" in meta.symbols_by_tag["test_tag"]

    def test_default_scale_class_constant(self, symbols_yaml):
        assert SymbolMetadata.DEFAULT_SCALE == 10_000

    def test_has_instrument_registry(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert hasattr(meta, "registry")
        from hft_platform.core.instrument_registry import InstrumentRegistry

        assert isinstance(meta.registry, InstrumentRegistry)

    def test_registry_populated_from_yaml(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert meta.registry.contains("TXFC0")
        assert meta.registry.contains("2330")
        assert meta.registry.contains("TXO22000C202604")
        p = meta.registry.get("TXFC0")
        assert p.instrument_type.value == "future"
        assert p.multiplier == 200

    def test_registry_reload_preserves_dynamic(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        from hft_platform.core.instrument_registry import (
            FeeStructure,
            InstrumentProfile,
            InstrumentType,
            TradingHours,
        )

        dynamic_profile = InstrumentProfile(
            symbol="DYN1",
            instrument_type=InstrumentType.OPTION,
            underlying="TX",
            exchange="TAIFEX",
            multiplier=50,
            tick_size_scaled=10000,
            price_scale=10000,
            fee_structure=FeeStructure(tax_rate_bps=20, commission_per_lot=130000),
            trading_hours=TradingHours(day_open="08:45", day_close="13:45", night_open=None, night_close=None),
        )
        meta.registry.register(dynamic_profile, source="dynamic")
        meta.reload()
        assert meta.registry.contains("DYN1")  # dynamic preserved
        assert meta.registry.contains("TXFC0")  # static re-loaded
