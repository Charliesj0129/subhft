"""Tests for FubonOrderGateway and FubonAccountGateway."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    def test_cancel_order(self) -> None:
        sdk = MagicMock()
        sdk.stock.cancel_order.return_value = {"status": "cancelled"}
        gw = FubonOrderGateway(sdk)

        result = gw.cancel_order("ORD-001")

        sdk.stock.cancel_order.assert_called_once_with(order_id="ORD-001")
        assert result == {"status": "cancelled"}

    def test_cancel_order_exception(self) -> None:
        sdk = MagicMock()
        sdk.stock.cancel_order.side_effect = RuntimeError("not found")
        gw = FubonOrderGateway(sdk)

        with pytest.raises(RuntimeError, match="not found"):
            gw.cancel_order("ORD-999")


class TestFubonOrderGatewayUpdate:
    def test_update_order(self) -> None:
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


class TestFubonOrderGatewayBatchPlaceOrders:
    def _make_order(
        self,
        symbol: str = "2330",
        price: int = 5800000,
        qty: int = 1,
        side: str = "Buy",
    ) -> dict:
        return {"symbol": symbol, "price": price, "qty": qty, "side": side}

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_all_succeed(self, mock_sleep: MagicMock) -> None:
        sdk = MagicMock()
        sdk.stock.place_order.side_effect = [
            {"order_id": "ORD-001"},
            {"order_id": "ORD-002"},
            {"order_id": "ORD-003"},
        ]
        gw = FubonOrderGateway(sdk)

        orders = [
            self._make_order("2330", 5800000, 1, "Buy"),
            self._make_order("2317", 1000000, 2, "Sell"),
            self._make_order("2454", 9000000, 3, "Buy"),
        ]
        results = gw.batch_place_orders(orders)

        assert len(results) == 3
        assert results[0] == {"order_id": "ORD-001"}
        assert results[1] == {"order_id": "ORD-002"}
        assert results[2] == {"order_id": "ORD-003"}
        assert sdk.stock.place_order.call_count == 3

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_partial_failure(self, mock_sleep: MagicMock) -> None:
        sdk = MagicMock()
        sdk.stock.place_order.side_effect = [
            {"order_id": "ORD-001"},
            RuntimeError("SDK error on 2nd"),
            {"order_id": "ORD-003"},
        ]
        gw = FubonOrderGateway(sdk)

        orders = [
            self._make_order("2330"),
            self._make_order("2317"),
            self._make_order("2454"),
        ]
        results = gw.batch_place_orders(orders)

        assert len(results) == 3
        assert results[0] == {"order_id": "ORD-001"}
        assert results[1] is None
        assert results[2] == {"order_id": "ORD-003"}

    def test_batch_empty_returns_empty(self) -> None:
        sdk = MagicMock()
        gw = FubonOrderGateway(sdk)

        results = gw.batch_place_orders([])

        assert results == []
        sdk.stock.place_order.assert_not_called()

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_single_order(self, mock_sleep: MagicMock) -> None:
        sdk = MagicMock()
        sdk.stock.place_order.return_value = {"order_id": "ORD-SINGLE"}
        gw = FubonOrderGateway(sdk)

        results = gw.batch_place_orders([self._make_order()])

        assert len(results) == 1
        assert results[0] == {"order_id": "ORD-SINGLE"}
        mock_sleep.assert_not_called()

    @patch("hft_platform.feed_adapter.fubon.order_gateway.time.sleep")
    def test_batch_rate_limit_delay(self, mock_sleep: MagicMock) -> None:
        sdk = MagicMock()
        sdk.stock.place_order.return_value = {"order_id": "ORD-X"}
        gw = FubonOrderGateway(sdk)

        orders = [self._make_order() for _ in range(3)]
        gw.batch_place_orders(orders)

        assert mock_sleep.call_count == 2  # Between orders 0-1 and 1-2
        mock_sleep.assert_called_with(0.067)
