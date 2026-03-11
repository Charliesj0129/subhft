"""Tests for Fubon order gateway."""

from __future__ import annotations

import sys
import types
from typing import Any, Protocol, runtime_checkable
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake fubon_neo module hierarchy so we can import FubonOrderGateway without
# the real SDK installed.
# ---------------------------------------------------------------------------


def _install_fake_fubon_neo() -> types.ModuleType:
    """Create and register a minimal fake ``fubon_neo`` package."""
    # Top-level package
    fubon_neo = types.ModuleType("fubon_neo")

    # fubon_neo.constant
    constant = types.ModuleType("fubon_neo.constant")

    class BSAction:
        Buy = "BUY"
        Sell = "SELL"

    class TimeInForce:
        ROD = "ROD"
        IOC = "IOC"
        FOK = "FOK"

    class PriceType:
        Limit = "LMT"
        Market = "MKT"

    class MarketType:
        Common = "COMMON"

    class OrderType:
        Stock = "STOCK"

    constant.BSAction = BSAction  # type: ignore[attr-defined]
    constant.TimeInForce = TimeInForce  # type: ignore[attr-defined]
    constant.PriceType = PriceType  # type: ignore[attr-defined]
    constant.MarketType = MarketType  # type: ignore[attr-defined]
    constant.OrderType = OrderType  # type: ignore[attr-defined]
    fubon_neo.constant = constant  # type: ignore[attr-defined]

    # fubon_neo.sdk
    sdk_mod = types.ModuleType("fubon_neo.sdk")

    class Order:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    sdk_mod.Order = Order  # type: ignore[attr-defined]
    fubon_neo.sdk = sdk_mod  # type: ignore[attr-defined]

    sys.modules["fubon_neo"] = fubon_neo
    sys.modules["fubon_neo.constant"] = constant
    sys.modules["fubon_neo.sdk"] = sdk_mod

    return fubon_neo


_install_fake_fubon_neo()

from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway  # noqa: E402

# ---------------------------------------------------------------------------
# OrderExecutor protocol (mirror of the spec)
# ---------------------------------------------------------------------------


@runtime_checkable
class OrderExecutor(Protocol):
    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: float,
        qty: int,
        order_type: str,
        tif: str,
        **kwargs: Any,
    ) -> Any: ...

    def cancel_order(self, trade: Any) -> Any: ...

    def update_order(self, trade: Any, price: float | None = None, qty: int | None = None) -> Any: ...

    def get_exchange(self, symbol: str) -> str: ...

    def set_execution_callbacks(
        self,
        on_order: Any,
        on_deal: Any,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_sdk() -> MagicMock:
    sdk = MagicMock()
    sdk.stock.place_order.return_value = {"order_no": "F001", "status": "submitted"}
    sdk.stock.cancel_order.return_value = {"order_no": "F001", "status": "cancelled"}
    sdk.stock.make_modify_price_obj.return_value = "modify_price_obj"
    sdk.stock.modify_price.return_value = {"order_no": "F001", "status": "modified"}
    sdk.stock.make_modify_volume_obj.return_value = "modify_vol_obj"
    sdk.stock.modify_volume.return_value = {"order_no": "F001", "status": "modified"}
    return sdk


@pytest.fixture()
def mock_account() -> MagicMock:
    return MagicMock(name="fubon_account")


@pytest.fixture()
def gateway(mock_sdk: MagicMock, mock_account: MagicMock) -> FubonOrderGateway:
    return FubonOrderGateway(
        sdk=mock_sdk,
        account=mock_account,
        symbols_meta={"2881": {"exchange": "TSE"}, "6116": {"exchange": "OTC"}},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def test_constructs_order_and_delegates(
        self, gateway: FubonOrderGateway, mock_sdk: MagicMock, mock_account: MagicMock
    ) -> None:
        result = gateway.place_order(
            contract_code="2881",
            exchange="TSE",
            action="Buy",
            price=660000,  # 66.0 x 10000
            qty=2000,
            order_type="LMT",
            tif="ROD",
        )
        assert result == {"order_no": "F001", "status": "submitted"}
        mock_sdk.stock.place_order.assert_called_once()
        args = mock_sdk.stock.place_order.call_args
        assert args[0][0] is mock_account  # first positional arg = account

        order_obj = args[0][1]
        assert order_obj.symbol == "2881"
        assert order_obj.quantity == 2000
        assert order_obj.buy_sell == "BUY"

    def test_converts_scaled_int_price_to_string(self, gateway: FubonOrderGateway, mock_sdk: MagicMock) -> None:
        gateway.place_order(
            contract_code="2881",
            exchange="TSE",
            action="Buy",
            price=665000,  # 66.5 x 10000
            qty=1,
            order_type="LMT",
            tif="ROD",
        )
        order_obj = mock_sdk.stock.place_order.call_args[0][1]
        assert order_obj.price == "66.5"

    def test_integer_price_no_trailing_zeros(self, gateway: FubonOrderGateway, mock_sdk: MagicMock) -> None:
        """660000 / 10000 = 66, should produce '66'."""
        gateway.place_order(
            contract_code="2881",
            exchange="TSE",
            action="Buy",
            price=660000,
            qty=1,
            order_type="LMT",
            tif="ROD",
        )
        order_obj = mock_sdk.stock.place_order.call_args[0][1]
        # Decimal('66') renders as '66'
        assert order_obj.price == "66"

    def test_market_order_price_none(self, gateway: FubonOrderGateway, mock_sdk: MagicMock) -> None:
        gateway.place_order(
            contract_code="2881",
            exchange="TSE",
            action="Buy",
            price=0,
            qty=1,
            order_type="MKT",
            tif="IOC",
        )
        order_obj = mock_sdk.stock.place_order.call_args[0][1]
        assert order_obj.price is None

    def test_sell_action(self, gateway: FubonOrderGateway, mock_sdk: MagicMock) -> None:
        gateway.place_order(
            contract_code="2881",
            exchange="TSE",
            action="Sell",
            price=660000,
            qty=500,
            order_type="LMT",
            tif="IOC",
        )
        order_obj = mock_sdk.stock.place_order.call_args[0][1]
        assert order_obj.buy_sell == "SELL"

    def test_custom_user_def(self, gateway: FubonOrderGateway, mock_sdk: MagicMock) -> None:
        gateway.place_order(
            contract_code="2881",
            exchange="TSE",
            action="Buy",
            price=660000,
            qty=1,
            order_type="LMT",
            tif="ROD",
            user_def="CUSTOM",
        )
        order_obj = mock_sdk.stock.place_order.call_args[0][1]
        assert order_obj.user_def == "CUSTOM"

    def test_default_user_def_is_hft(self, gateway: FubonOrderGateway, mock_sdk: MagicMock) -> None:
        gateway.place_order(
            contract_code="2881",
            exchange="TSE",
            action="Buy",
            price=660000,
            qty=1,
            order_type="LMT",
            tif="ROD",
        )
        order_obj = mock_sdk.stock.place_order.call_args[0][1]
        assert order_obj.user_def == "HFT"


class TestCancelOrder:
    def test_delegates_to_sdk(self, gateway: FubonOrderGateway, mock_sdk: MagicMock, mock_account: MagicMock) -> None:
        trade = MagicMock(name="trade_to_cancel")
        result = gateway.cancel_order(trade)
        assert result == {"order_no": "F001", "status": "cancelled"}
        mock_sdk.stock.cancel_order.assert_called_once_with(mock_account, trade)


class TestUpdateOrder:
    def test_modify_price(self, gateway: FubonOrderGateway, mock_sdk: MagicMock, mock_account: MagicMock) -> None:
        trade = MagicMock(name="existing_trade")
        result = gateway.update_order(trade, price=675000)  # 67.5
        mock_sdk.stock.make_modify_price_obj.assert_called_once_with(trade, "67.5")
        mock_sdk.stock.modify_price.assert_called_once_with(mock_account, "modify_price_obj")
        assert result == {"order_no": "F001", "status": "modified"}

    def test_modify_qty(self, gateway: FubonOrderGateway, mock_sdk: MagicMock, mock_account: MagicMock) -> None:
        trade = MagicMock(name="existing_trade")
        result = gateway.update_order(trade, qty=500)
        mock_sdk.stock.make_modify_volume_obj.assert_called_once_with(trade, 500)
        mock_sdk.stock.modify_volume.assert_called_once_with(mock_account, "modify_vol_obj")
        assert result == {"order_no": "F001", "status": "modified"}

    def test_no_change_returns_none(self, gateway: FubonOrderGateway) -> None:
        trade = MagicMock()
        result = gateway.update_order(trade)
        assert result is None


class TestGetExchange:
    def test_returns_exchange_from_metadata(self, gateway: FubonOrderGateway) -> None:
        assert gateway.get_exchange("2881") == "TSE"
        assert gateway.get_exchange("6116") == "OTC"

    def test_defaults_to_tse(self, gateway: FubonOrderGateway) -> None:
        assert gateway.get_exchange("9999") == "TSE"


class TestSetExecutionCallbacks:
    def test_stores_callbacks(self, gateway: FubonOrderGateway) -> None:
        on_order = MagicMock()
        on_deal = MagicMock()
        gateway.set_execution_callbacks(on_order, on_deal)
        assert gateway._on_order_cb is on_order
        assert gateway._on_deal_cb is on_deal


class TestProtocolConformance:
    def test_isinstance_order_executor(self, gateway: FubonOrderGateway) -> None:
        assert isinstance(gateway, OrderExecutor)
