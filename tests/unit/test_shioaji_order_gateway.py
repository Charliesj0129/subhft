"""Tests for OrderGateway non-blocking order placement."""

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway


@pytest.fixture()
def _mock_env():
    """Suppress session refresh thread during tests."""
    with patch.dict(os.environ, {"HFT_SESSION_REFRESH_S": "0"}):
        yield


@pytest.fixture()
def mock_sj():
    """Patch the shioaji SDK module-level symbol."""
    with patch("hft_platform.feed_adapter.shioaji_client.sj") as sj:
        sj.constant.Action.Buy = "Buy"
        sj.constant.Action.Sell = "Sell"
        sj.constant.StockPriceType.LMT = "LMT"
        sj.constant.OrderType.ROD = "ROD"
        sj.constant.OrderType.IOC = "IOC"
        sj.constant.OrderType.FOK = "FOK"
        sj.Order = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
        yield sj


@pytest.fixture()
def gateway(_mock_env, mock_sj) -> OrderGateway:
    """Create an OrderGateway with a mocked ShioajiClient."""
    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yaml")
    yaml.dump(
        {"symbols": [{"code": "2330", "exchange": "TSE"}]},
        tmp,
    )
    tmp.close()
    try:
        from hft_platform.feed_adapter.shioaji_client import ShioajiClient

        client = ShioajiClient(config_path=tmp.name)
        mock_api = MagicMock()
        client.api = mock_api
        client.metrics = MagicMock()

        # Set up contract lookup
        mock_contract = MagicMock()
        mock_contract.code = "2330"
        mock_api.Contracts.Stocks.TSE = {"2330": mock_contract}

        gw = OrderGateway(client)
        yield gw
        client.close()
    finally:
        os.unlink(tmp.name)


def _get_api(gw: OrderGateway) -> MagicMock:
    return gw._client.api


# ---------------------------------------------------------------------------
# place_order tests
# ---------------------------------------------------------------------------


class TestPlaceOrderBlocking:
    """Default timeout=5000 preserves blocking behavior (no extra kwargs)."""

    def test_blocking_place_order_no_extra_kwargs(self, gateway: OrderGateway, mock_sj: Any) -> None:
        api = _get_api(gateway)
        api.place_order.return_value = {"status": "ok"}

        result = gateway.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
        )

        assert result == {"status": "ok"}
        call_args = api.place_order.call_args
        # Should NOT pass timeout or cb kwargs
        assert "timeout" not in call_args.kwargs
        assert "cb" not in call_args.kwargs

    def test_explicit_timeout_5000_no_extra_kwargs(self, gateway: OrderGateway, mock_sj: Any) -> None:
        api = _get_api(gateway)
        api.place_order.return_value = {"status": "ok"}

        gateway.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=5000,
        )

        call_args = api.place_order.call_args
        assert "timeout" not in call_args.kwargs
        assert "cb" not in call_args.kwargs


class TestPlaceOrderNonBlocking:
    """timeout=0 enables non-blocking order with optional callback."""

    def test_nonblocking_passes_timeout_zero(self, gateway: OrderGateway, mock_sj: Any) -> None:
        api = _get_api(gateway)
        api.place_order.return_value = None

        gateway.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=0,
        )

        call_args = api.place_order.call_args
        assert call_args.kwargs.get("timeout") == 0

    def test_nonblocking_with_callback(self, gateway: OrderGateway, mock_sj: Any) -> None:
        api = _get_api(gateway)
        api.place_order.return_value = None
        my_cb = MagicMock()

        gateway.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=0,
            cb=my_cb,
        )

        call_args = api.place_order.call_args
        assert call_args.kwargs.get("timeout") == 0
        assert call_args.kwargs.get("cb") is my_cb

    def test_nonblocking_without_callback_omits_cb(self, gateway: OrderGateway, mock_sj: Any) -> None:
        api = _get_api(gateway)
        api.place_order.return_value = None

        gateway.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=0,
            cb=None,
        )

        call_args = api.place_order.call_args
        assert call_args.kwargs.get("timeout") == 0
        assert "cb" not in call_args.kwargs

    def test_nonblocking_does_not_raise(self, gateway: OrderGateway, mock_sj: Any) -> None:
        """Non-blocking place_order completes without error and records latency."""
        api = _get_api(gateway)
        api.place_order.return_value = None

        # Should not raise; latency is recorded via _record_api_latency internally
        result = gateway.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=0,
        )
        assert result is None
        api.place_order.assert_called_once()


# ---------------------------------------------------------------------------
# cancel_order tests
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_blocking_cancel_no_extra_kwargs(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()
        api.cancel_order.return_value = {"status": "cancelled"}

        result = gateway.cancel_order(trade)

        assert result == {"status": "cancelled"}
        call_args = api.cancel_order.call_args
        assert "timeout" not in call_args.kwargs
        assert "cb" not in call_args.kwargs

    def test_nonblocking_cancel_passes_timeout_zero(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()

        gateway.cancel_order(trade, timeout=0)

        call_args = api.cancel_order.call_args
        assert call_args.kwargs.get("timeout") == 0

    def test_nonblocking_cancel_with_callback(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()
        my_cb = MagicMock()

        gateway.cancel_order(trade, timeout=0, cb=my_cb)

        call_args = api.cancel_order.call_args
        assert call_args.kwargs.get("timeout") == 0
        assert call_args.kwargs.get("cb") is my_cb


# ---------------------------------------------------------------------------
# update_order tests
# ---------------------------------------------------------------------------


class TestUpdateOrder:
    def test_blocking_update_price_no_extra_kwargs(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()
        api.update_order.return_value = {"status": "updated"}

        result = gateway.update_order(trade, price=105.0)

        assert result == {"status": "updated"}
        call_args = api.update_order.call_args
        assert "timeout" not in call_args.kwargs
        assert "cb" not in call_args.kwargs

    def test_nonblocking_update_price_passes_timeout_zero(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()

        gateway.update_order(trade, price=105.0, timeout=0)

        call_args = api.update_order.call_args
        assert call_args.kwargs.get("timeout") == 0

    def test_nonblocking_update_price_with_callback(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()
        my_cb = MagicMock()

        gateway.update_order(trade, price=105.0, timeout=0, cb=my_cb)

        call_args = api.update_order.call_args
        assert call_args.kwargs.get("timeout") == 0
        assert call_args.kwargs.get("cb") is my_cb

    def test_blocking_update_qty_no_extra_kwargs(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()
        api.update_order.return_value = {"status": "updated"}

        result = gateway.update_order(trade, qty=5)

        assert result == {"status": "updated"}
        call_args = api.update_order.call_args
        assert "timeout" not in call_args.kwargs

    def test_nonblocking_update_qty_with_callback(self, gateway: OrderGateway) -> None:
        api = _get_api(gateway)
        trade = MagicMock()
        my_cb = MagicMock()

        gateway.update_order(trade, qty=5, timeout=0, cb=my_cb)

        call_args = api.update_order.call_args
        assert call_args.kwargs.get("timeout") == 0
        assert call_args.kwargs.get("cb") is my_cb

    def test_update_no_price_no_qty_returns_none(self, gateway: OrderGateway) -> None:
        result = gateway.update_order(MagicMock())
        assert result is None
