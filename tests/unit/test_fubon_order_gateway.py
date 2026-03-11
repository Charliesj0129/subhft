"""Tests for FubonOrderGateway and FubonAccountGateway."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.unit.fubon_mock_helper import install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway
from hft_platform.feed_adapter.fubon.order_gateway import PRICE_SCALE, FubonOrderGateway


class TestFubonOrderGatewayPlaceOrder:
    def test_place_order_calls_sdk_with_unscaled_price(self) -> None:
        sdk = MagicMock()
        sdk.stock.place_order.return_value = {"order_id": "ORD-001"}
        gw = FubonOrderGateway(sdk)

        result = gw.place_order(
            symbol="2330",
            price=5800000,  # 580.0 * 10000
            qty=1,
            side="Buy",
        )

        sdk.stock.place_order.assert_called_once()
        call_kwargs = sdk.stock.place_order.call_args
        assert call_kwargs.kwargs["price"] == 5800000 / PRICE_SCALE
        assert call_kwargs.kwargs["symbol"] == "2330"
        assert call_kwargs.kwargs["quantity"] == 1
        assert result == {"order_id": "ORD-001"}

    def test_place_order_sell_ioc_market(self) -> None:
        sdk = MagicMock()
        sdk.stock.place_order.return_value = {"order_id": "ORD-002"}
        gw = FubonOrderGateway(sdk)

        gw.place_order(
            symbol="2317",
            price=1000000,
            qty=5,
            side="Sell",
            tif="IOC",
            price_type="MKT",
        )

        call_kwargs = sdk.stock.place_order.call_args.kwargs
        assert call_kwargs["buy_sell"] == "FUBON_SELL"
        assert call_kwargs["time_in_force"] == "FUBON_IOC"
        assert call_kwargs["price_type"] == "FUBON_MARKET"

    def test_place_order_sdk_exception_propagates(self) -> None:
        sdk = MagicMock()
        sdk.stock.place_order.side_effect = RuntimeError("SDK error")
        gw = FubonOrderGateway(sdk)

        with pytest.raises(RuntimeError, match="SDK error"):
            gw.place_order(symbol="2330", price=5800000, qty=1, side="Buy")

    def test_place_order_price_unscaling_precision(self) -> None:
        """Verify price unscaling: 1234567 / 10000 = 123.4567."""
        sdk = MagicMock()
        sdk.stock.place_order.return_value = {}
        gw = FubonOrderGateway(sdk)

        gw.place_order(symbol="2330", price=1234567, qty=1, side="Buy")

        call_kwargs = sdk.stock.place_order.call_args.kwargs
        assert abs(call_kwargs["price"] - 123.4567) < 1e-10


class TestFubonOrderGatewayFutopt:
    def test_place_futopt_order(self) -> None:
        sdk = MagicMock()
        sdk.futopt.place_order.return_value = {"order_id": "FUT-001"}
        gw = FubonOrderGateway(sdk)

        result = gw.place_futopt_order(
            symbol="TXFA4",
            price=200000000,  # 20000.0 * 10000
            qty=2,
            side="Sell",
            tif="FOK",
        )

        sdk.futopt.place_order.assert_called_once()
        call_kwargs = sdk.futopt.place_order.call_args.kwargs
        assert call_kwargs["price"] == 200000000 / PRICE_SCALE
        assert call_kwargs["buy_sell"] == "FUBON_SELL"
        assert call_kwargs["time_in_force"] == "FUBON_FOK"
        assert result == {"order_id": "FUT-001"}

    def test_place_futopt_order_exception(self) -> None:
        sdk = MagicMock()
        sdk.futopt.place_order.side_effect = ConnectionError("timeout")
        gw = FubonOrderGateway(sdk)

        with pytest.raises(ConnectionError, match="timeout"):
            gw.place_futopt_order(symbol="TXFA4", price=200000000, qty=1, side="Buy")


class TestFubonOrderGatewayCancel:
    def test_cancel_order_with_string_order_id(self) -> None:
        """Backward compat: pass raw order_id string."""
        sdk = MagicMock()
        sdk.stock.cancel_order.return_value = {"status": "cancelled"}
        gw = FubonOrderGateway(sdk)

        result = gw.cancel_order("ORD-001")

        sdk.stock.cancel_order.assert_called_once_with(order_id="ORD-001")
        assert result == {"status": "cancelled"}

    def test_cancel_order_with_trade_object(self) -> None:
        """BrokerProtocol compatible: pass trade object with order_id attr."""
        sdk = MagicMock()
        sdk.stock.cancel_order.return_value = {"status": "cancelled"}
        gw = FubonOrderGateway(sdk)
        trade = MagicMock()
        trade.order_id = "ORD-002"

        result = gw.cancel_order(trade)

        sdk.stock.cancel_order.assert_called_once_with(order_id="ORD-002")
        assert result == {"status": "cancelled"}

    def test_cancel_order_none_raises_type_error(self) -> None:
        """Passing None should raise TypeError, not reach SDK."""
        sdk = MagicMock()
        gw = FubonOrderGateway(sdk)

        with pytest.raises(TypeError, match="cannot extract order_id"):
            gw.cancel_order(None)

        sdk.stock.cancel_order.assert_not_called()

    def test_cancel_order_object_without_order_id_raises(self) -> None:
        """Trade object missing order_id attribute should raise TypeError."""
        sdk = MagicMock()
        gw = FubonOrderGateway(sdk)
        trade = MagicMock(spec=[])  # no attributes

        with pytest.raises(TypeError, match="cannot extract order_id"):
            gw.cancel_order(trade)

    def test_cancel_order_exception(self) -> None:
        sdk = MagicMock()
        sdk.stock.cancel_order.side_effect = RuntimeError("not found")
        gw = FubonOrderGateway(sdk)

        with pytest.raises(RuntimeError, match="not found"):
            gw.cancel_order("ORD-999")


class TestFubonOrderGatewayUpdate:
    def test_update_order_with_string_order_id(self) -> None:
        """Backward compat: pass raw order_id string."""
        sdk = MagicMock()
        sdk.stock.modify_order.return_value = {"status": "modified"}
        gw = FubonOrderGateway(sdk)

        result = gw.update_order("ORD-001", price=6000000, qty=3)

        sdk.stock.modify_order.assert_called_once_with(
            order_id="ORD-001",
            price=6000000 / PRICE_SCALE,
            quantity=3,
        )
        assert result == {"status": "modified"}

    def test_update_order_with_trade_object(self) -> None:
        """BrokerProtocol compatible: pass trade object with order_id attr."""
        sdk = MagicMock()
        sdk.stock.modify_order.return_value = {"status": "modified"}
        gw = FubonOrderGateway(sdk)
        trade = MagicMock()
        trade.order_id = "ORD-003"

        result = gw.update_order(trade, price=7000000, qty=5)

        sdk.stock.modify_order.assert_called_once_with(
            order_id="ORD-003",
            price=7000000 / PRICE_SCALE,
            quantity=5,
        )
        assert result == {"status": "modified"}

    def test_update_order_price_only(self) -> None:
        """Only price provided — qty should not be passed to SDK."""
        sdk = MagicMock()
        sdk.stock.modify_order.return_value = {"status": "modified"}
        gw = FubonOrderGateway(sdk)

        gw.update_order("ORD-001", price=6000000)

        sdk.stock.modify_order.assert_called_once_with(
            order_id="ORD-001",
            price=6000000 / PRICE_SCALE,
        )

    def test_update_order_qty_only(self) -> None:
        """Only qty provided — price should not be passed to SDK."""
        sdk = MagicMock()
        sdk.stock.modify_order.return_value = {"status": "modified"}
        gw = FubonOrderGateway(sdk)

        gw.update_order("ORD-001", qty=10)

        sdk.stock.modify_order.assert_called_once_with(
            order_id="ORD-001",
            quantity=10,
        )

    def test_update_order_no_changes_returns_none(self) -> None:
        """No price or qty provided — should log warning and return None."""
        sdk = MagicMock()
        gw = FubonOrderGateway(sdk)

        result = gw.update_order("ORD-001")

        assert result is None
        sdk.stock.modify_order.assert_not_called()

    def test_update_order_none_trade_raises_type_error(self) -> None:
        """Passing None as trade with valid price should raise TypeError."""
        sdk = MagicMock()
        gw = FubonOrderGateway(sdk)

        with pytest.raises(TypeError, match="cannot extract order_id"):
            gw.update_order(None, price=6000000)

    def test_update_order_exception(self) -> None:
        sdk = MagicMock()
        sdk.stock.modify_order.side_effect = ValueError("invalid qty")
        gw = FubonOrderGateway(sdk)

        with pytest.raises(ValueError, match="invalid qty"):
            gw.update_order("ORD-001", price=5800000, qty=-1)


class TestFubonAccountGateway:
    def test_get_inventories(self) -> None:
        sdk = MagicMock()
        sdk.stock.inventories.return_value = [{"symbol": "2330", "qty": 100}]
        gw = FubonAccountGateway(sdk)

        result = gw.get_inventories()

        sdk.stock.inventories.assert_called_once()
        assert len(result) == 1
        assert result[0]["symbol"] == "2330"

    def test_get_inventories_exception(self) -> None:
        sdk = MagicMock()
        sdk.stock.inventories.side_effect = RuntimeError("auth failed")
        gw = FubonAccountGateway(sdk)

        with pytest.raises(RuntimeError, match="auth failed"):
            gw.get_inventories()

    def test_get_accounting(self) -> None:
        sdk = MagicMock()
        sdk.accounting.return_value = {"balance": 1000000}
        gw = FubonAccountGateway(sdk)

        result = gw.get_accounting()

        sdk.accounting.assert_called_once()
        assert result["balance"] == 1000000

    def test_get_accounting_exception(self) -> None:
        sdk = MagicMock()
        sdk.accounting.side_effect = RuntimeError("timeout")
        gw = FubonAccountGateway(sdk)

        with pytest.raises(RuntimeError, match="timeout"):
            gw.get_accounting()

    def test_get_margin(self) -> None:
        sdk = MagicMock()
        sdk.futopt_accounting.return_value = {"margin_available": 500000}
        gw = FubonAccountGateway(sdk)

        result = gw.get_margin()

        sdk.futopt_accounting.assert_called_once()
        assert result["margin_available"] == 500000

    def test_get_margin_exception(self) -> None:
        sdk = MagicMock()
        sdk.futopt_accounting.side_effect = RuntimeError("no account")
        gw = FubonAccountGateway(sdk)

        with pytest.raises(RuntimeError, match="no account"):
            gw.get_margin()

    def test_get_settlements(self) -> None:
        sdk = MagicMock()
        sdk.settlements.return_value = [{"date": "2026-03-10", "amount": 50000}]
        gw = FubonAccountGateway(sdk)

        result = gw.get_settlements()

        sdk.settlements.assert_called_once()
        assert len(result) == 1
