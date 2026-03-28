"""Tests for per-product-type price caps in PriceBandValidator (S2)."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core.pricing import SymbolMetadataPriceScaleProvider
from hft_platform.risk.validators import PriceBandValidator


def _intent(symbol: str = "AAA", price: int = 10000, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="strat",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        target_order_id=None,
        timestamp_ns=0,
    )


def _make_metadata_provider(product_type_map: dict[str, str]) -> SymbolMetadataPriceScaleProvider:
    """Create a provider with a mock SymbolMetadata that returns product types."""
    metadata = MagicMock()
    metadata.price_scale.return_value = 10000
    metadata.product_type.side_effect = lambda sym: product_type_map.get(sym, "")
    provider = SymbolMetadataPriceScaleProvider(metadata=metadata)
    return provider


class TestProductTypePriceCaps:
    def test_futures_price_passes_with_futures_cap(self):
        """TMFD6 at 33000 NTD passes when max_price_cap_futures=50000."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("TMFD6", price=33000 * 10000))
        assert ok, f"Expected pass, got: {reason}"

    def test_futures_price_rejected_without_futures_cap(self):
        """TMFD6 at 33000 NTD rejected by default 5000 cap."""
        cfg = {"global_defaults": {"max_price_cap": 5000.0}}
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("TMFD6", price=33000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_stock_price_uses_global_cap(self):
        """Stock at 4000 NTD passes global 5000 cap."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"2330": "stock"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, _ = v.check(_intent("2330", price=4000 * 10000))
        assert ok

    def test_stock_price_rejected_above_global_cap(self):
        """Stock at 6000 NTD rejected by global 5000 cap (not lifted by futures cap)."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"2330": "stock"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("2330", price=6000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_per_symbol_override_beats_product_type(self):
        """Per-symbol cap overrides per-product-type cap."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
                "max_price_cap_TXFD6": 40000.0,
            }
        }
        provider = _make_metadata_provider({"TXFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("TXFD6", price=41000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_options_cap(self):
        """Options product type uses max_price_cap_options."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_options": 10000.0,
            }
        }
        provider = _make_metadata_provider({"TXO001": "option"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, _ = v.check(_intent("TXO001", price=8000 * 10000))
        assert ok

    def test_unknown_product_type_falls_back_to_global(self):
        """Unknown product type uses global cap."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"UNKNOWN": ""})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("UNKNOWN", price=6000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_cap_cached_per_symbol(self):
        """Second call for same symbol uses cache (no re-resolution)."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        v.check(_intent("TMFD6", price=33000 * 10000))
        assert "TMFD6" in v._max_price_scaled_cache
        ok, _ = v.check(_intent("TMFD6", price=33000 * 10000))
        assert ok

    def test_reason_includes_symbol(self):
        """Rejection reason includes the symbol name for observability (S5c)."""
        cfg = {"global_defaults": {"max_price_cap": 5000.0}}
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("TMFD6", price=33000 * 10000))
        assert not ok
        assert "TMFD6" in reason
