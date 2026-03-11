from __future__ import annotations

import pytest

from hft_platform.broker.exec_field_map import (
    FUBON_FIELD_MAP,
    SHIOAJI_FIELD_MAP,
    BrokerExecFieldMap,
)


class TestShioajiResolveOrderId:
    def test_shioaji_resolve_order_id(self) -> None:
        assert SHIOAJI_FIELD_MAP.resolve_order_id({"ordno": "ABC123"}) == "ABC123"

    def test_shioaji_resolve_order_id_fallback(self) -> None:
        assert SHIOAJI_FIELD_MAP.resolve_order_id({"seq_no": "456"}) == "456"

    def test_shioaji_resolve_order_id_empty(self) -> None:
        assert SHIOAJI_FIELD_MAP.resolve_order_id({}) == ""


class TestShioajiResolveStrategyId:
    def test_shioaji_resolve_strategy_id(self) -> None:
        assert SHIOAJI_FIELD_MAP.resolve_strategy_id({"custom_field": "STR001"}) == "STR001"


class TestShioajiResolveSymbol:
    def test_shioaji_resolve_symbol_nested(self) -> None:
        data: dict[str, object] = {"contract": {"code": "2330"}}
        assert SHIOAJI_FIELD_MAP.resolve_symbol(data) == "2330"


class TestShioajiAction:
    def test_shioaji_is_buy(self) -> None:
        assert SHIOAJI_FIELD_MAP.is_buy({"action": "Buy"}) is True

    def test_shioaji_is_sell(self) -> None:
        assert SHIOAJI_FIELD_MAP.is_sell({"action": "Sell"}) is True

    def test_shioaji_is_buy_wrong_action(self) -> None:
        assert SHIOAJI_FIELD_MAP.is_buy({"action": "Sell"}) is False


class TestFubonResolveOrderId:
    def test_fubon_resolve_order_id(self) -> None:
        assert FUBON_FIELD_MAP.resolve_order_id({"ord_no": "F123"}) == "F123"


class TestFubonResolveStrategyId:
    def test_fubon_resolve_strategy_id(self) -> None:
        assert FUBON_FIELD_MAP.resolve_strategy_id({"user_def": "MY_STRAT"}) == "MY_STRAT"


class TestFubonResolveSymbol:
    def test_fubon_resolve_symbol_flat(self) -> None:
        data: dict[str, object] = {"stock_no": "2330"}
        assert FUBON_FIELD_MAP.resolve_symbol(data) == "2330"


class TestFubonAction:
    def test_fubon_is_buy_b(self) -> None:
        assert FUBON_FIELD_MAP.is_buy({"buy_sell": "B"}) is True

    def test_fubon_is_buy_full(self) -> None:
        assert FUBON_FIELD_MAP.is_buy({"buy_sell": "Buy"}) is True

    def test_fubon_is_sell(self) -> None:
        assert FUBON_FIELD_MAP.is_sell({"buy_sell": "S"}) is True


class TestFieldMapProperties:
    def test_field_map_frozen(self) -> None:
        fm = BrokerExecFieldMap()
        with pytest.raises(AttributeError):
            fm.price_field = "px"  # type: ignore[misc]

    def test_field_map_slots(self) -> None:
        assert hasattr(BrokerExecFieldMap, "__slots__")

    def test_resolve_symbol_with_object(self) -> None:
        """Test attribute access path (not just dict)."""

        class Contract:
            __slots__ = ("code",)

            def __init__(self, code: str) -> None:
                self.code = code

        data: dict[str, object] = {"contract": Contract("2317")}
        assert SHIOAJI_FIELD_MAP.resolve_symbol(data) == "2317"
