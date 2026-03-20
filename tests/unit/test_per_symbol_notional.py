"""Tests for PerSymbolNotionalValidator (WU-05)."""

from __future__ import annotations

from typing import Any, Dict

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.validators import PerSymbolNotionalValidator
from tests.factories import make_order_intent


def _make_intent(
    *,
    strategy_id: str = "strat_a",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
    price: int = 5000000,
    qty: int = 10,
    side: Side = Side.BUY,
) -> OrderIntent:
    return make_order_intent(
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        price=price,
        qty=qty,
        side=side,
    )


def _make_config(
    *,
    global_default: int | None = None,
    strategies: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    defaults: Dict[str, Any] = {}
    if global_default is not None:
        defaults["per_symbol_max_notional"] = global_default
    config["global_defaults"] = defaults
    if strategies is not None:
        config["strategies"] = strategies
    return config


class TestPerSymbolNotional:
    """Tests for PerSymbolNotionalValidator."""

    def test_cancel_always_passes(self) -> None:
        config = _make_config(global_default=1)  # tiny limit
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(intent_type=IntentType.CANCEL, price=999999999, qty=999)
        ok, reason = v.check(intent)
        assert ok is True
        assert reason == "OK"

    def test_within_global_default_passes(self) -> None:
        # price=5_000_000 (500 scaled), qty=10 => notional_scaled = 50_000_000
        # global default = 50_000_000 (unscaled) * 10000 (scale) = 500_000_000_000
        config = _make_config(global_default=50_000_000)
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(price=5_000_000, qty=10)
        ok, reason = v.check(intent)
        assert ok is True

    def test_exceeds_global_default_rejected(self) -> None:
        # Set a very low global limit: 1 (unscaled) * 10000 = 10000 scaled
        # Intent notional: 5_000_000 * 10 = 50_000_000 > 10_000
        config = _make_config(global_default=1)
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(price=5_000_000, qty=10)
        ok, reason = v.check(intent)
        assert ok is False
        assert "PER_SYMBOL_NOTIONAL_EXCEEDED" in reason

    def test_per_strategy_symbol_override(self) -> None:
        # Global: very high, but strategy-symbol override is low
        config = _make_config(
            global_default=999_999_999,
            strategies={
                "strat_a": {
                    "symbol_limits": {
                        "2330": {"max_notional": 1},  # 1 unscaled => 10000 scaled
                    },
                },
            },
        )
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(strategy_id="strat_a", symbol="2330", price=5_000_000, qty=10)
        ok, reason = v.check(intent)
        assert ok is False
        assert "PER_SYMBOL_NOTIONAL_EXCEEDED" in reason

    def test_per_strategy_symbol_override_passes(self) -> None:
        # Strategy-symbol override allows large notional
        config = _make_config(
            global_default=1,  # global is low
            strategies={
                "strat_a": {
                    "symbol_limits": {
                        "2330": {"max_notional": 999_999_999},
                    },
                },
            },
        )
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(strategy_id="strat_a", symbol="2330", price=5_000_000, qty=10)
        ok, reason = v.check(intent)
        assert ok is True

    def test_fallback_when_no_symbol_limit(self) -> None:
        """Strategy has symbol_limits but not for this symbol — falls back to global."""
        config = _make_config(
            global_default=1,  # very low
            strategies={
                "strat_a": {
                    "symbol_limits": {
                        "9999": {"max_notional": 999_999_999},  # different symbol
                    },
                },
            },
        )
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(strategy_id="strat_a", symbol="2330")
        ok, reason = v.check(intent)
        assert ok is False
        assert "PER_SYMBOL_NOTIONAL_EXCEEDED" in reason

    def test_cache_is_used(self) -> None:
        """Second check for same key should use cached value."""
        config = _make_config(global_default=999_999_999)
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent()
        v.check(intent)
        assert len(v._per_symbol_notional_cache) == 1
        # Second check — cache hit
        v.check(intent)
        assert len(v._per_symbol_notional_cache) == 1

    def test_cache_overflow_eviction(self) -> None:
        """Cache clears on overflow."""
        config = _make_config(global_default=999_999_999)
        v = PerSymbolNotionalValidator(config)
        v._MAX_CACHE_ENTRIES = 3  # small for testing

        for i in range(4):
            intent = _make_intent(symbol=f"SYM{i}")
            v.check(intent)

        # After overflow the cache was cleared then the new entry was added
        assert len(v._per_symbol_notional_cache) == 1

    def test_clear_cache(self) -> None:
        config = _make_config(global_default=999_999_999)
        v = PerSymbolNotionalValidator(config)
        v.check(_make_intent())
        assert len(v._per_symbol_notional_cache) == 1
        v.clear_cache()
        assert len(v._per_symbol_notional_cache) == 0

    def test_amend_also_validated(self) -> None:
        """AMEND orders should still be validated (not just NEW)."""
        config = _make_config(global_default=1)
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(intent_type=IntentType.AMEND, price=5_000_000, qty=10)
        ok, reason = v.check(intent)
        assert ok is False
        assert "PER_SYMBOL_NOTIONAL_EXCEEDED" in reason

    def test_zero_qty_passes(self) -> None:
        """Zero qty => notional = 0, always passes."""
        config = _make_config(global_default=1)
        v = PerSymbolNotionalValidator(config)
        intent = _make_intent(price=5_000_000, qty=0)
        ok, reason = v.check(intent)
        assert ok is True

    def test_hardcoded_fallback_when_no_global_default(self) -> None:
        """When no global_defaults.per_symbol_max_notional, uses 50_000_000."""
        config: Dict[str, Any] = {"global_defaults": {}, "strategies": {}}
        v = PerSymbolNotionalValidator(config)
        assert v._default_per_symbol_max_notional_raw == 50_000_000
