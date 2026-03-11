"""Tests for non-blocking order execution (timeout=0) plumbing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def _mock_sdk():
    """Patch the shioaji SDK module-level symbol used by OrderGateway._sdk()."""
    sdk = MagicMock()
    sdk.constant.Action.Buy = "BUY"
    sdk.constant.Action.Sell = "SELL"
    sdk.constant.StockPriceType.LMT = "LMT"
    sdk.constant.OrderType.ROD = "ROD"
    sdk.constant.OrderType.IOC = "IOC"
    sdk.constant.OrderType.FOK = "FOK"
    sdk.Order.return_value = MagicMock(name="order_obj")
    with patch(
        "hft_platform.feed_adapter.shioaji.order_gateway.OrderGateway._sdk",
        return_value=sdk,
    ):
        yield sdk


def _make_gateway(api: MagicMock | None = None) -> "OrderGateway":
    from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway

    client = MagicMock()
    client.api = api or MagicMock()
    client._get_contract.return_value = MagicMock(name="contract")
    gw = OrderGateway(client)
    return gw


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------

class TestPlaceOrderTimeout:
    """place_order forwards timeout to SDK and uses correct metric label."""

    def test_timeout_zero_passes_to_sdk(self, _mock_sdk: MagicMock) -> None:
        gw = _make_gateway()
        gw.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=0,
        )
        # SDK place_order must receive timeout=0
        gw._client.api.place_order.assert_called_once()
        _args, kwargs = gw._client.api.place_order.call_args
        assert kwargs["timeout"] == 0

    def test_timeout_none_no_timeout_kwarg(self, _mock_sdk: MagicMock) -> None:
        gw = _make_gateway()
        gw.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=None,
        )
        _args, kwargs = gw._client.api.place_order.call_args
        assert "timeout" not in kwargs

    def test_timeout_zero_metric_label(self, _mock_sdk: MagicMock) -> None:
        gw = _make_gateway()
        gw.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=0,
        )
        gw._client._record_api_latency.assert_called_once()
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "place_order_nb"

    def test_timeout_none_metric_label(self, _mock_sdk: MagicMock) -> None:
        gw = _make_gateway()
        gw.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=None,
        )
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "place_order"

    def test_timeout_positive_passes_to_sdk(self, _mock_sdk: MagicMock) -> None:
        gw = _make_gateway()
        gw.place_order(
            contract_code="2330",
            exchange="TSE",
            action="Buy",
            price=100.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            timeout=5,
        )
        _args, kwargs = gw._client.api.place_order.call_args
        assert kwargs["timeout"] == 5
        # Non-zero timeout still uses standard label
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "place_order"


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrderTimeout:
    """cancel_order forwards timeout to SDK."""

    def test_timeout_zero_passes_to_sdk(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.cancel_order(trade, timeout=0)
        _args, kwargs = gw._client.api.cancel_order.call_args
        assert kwargs["timeout"] == 0

    def test_timeout_none_no_timeout_kwarg(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.cancel_order(trade, timeout=None)
        _args, kwargs = gw._client.api.cancel_order.call_args
        assert "timeout" not in kwargs

    def test_timeout_zero_metric_label(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.cancel_order(trade, timeout=0)
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "cancel_order_nb"

    def test_timeout_none_metric_label(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.cancel_order(trade, timeout=None)
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "cancel_order"


# ---------------------------------------------------------------------------
# update_order
# ---------------------------------------------------------------------------

class TestUpdateOrderTimeout:
    """update_order forwards timeout to SDK."""

    def test_update_price_timeout_zero(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.update_order(trade, price=50.0, timeout=0)
        _args, kwargs = gw._client.api.update_order.call_args
        assert kwargs["timeout"] == 0
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "update_order_nb"

    def test_update_price_timeout_none(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.update_order(trade, price=50.0, timeout=None)
        _args, kwargs = gw._client.api.update_order.call_args
        assert "timeout" not in kwargs
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "update_order"

    def test_update_qty_timeout_zero(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.update_order(trade, qty=10, timeout=0)
        _args, kwargs = gw._client.api.update_order.call_args
        assert kwargs["timeout"] == 0
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "update_order_nb"

    def test_update_qty_timeout_none(self) -> None:
        gw = _make_gateway()
        trade = MagicMock()
        gw.update_order(trade, qty=10, timeout=None)
        _args, kwargs = gw._client.api.update_order.call_args
        assert "timeout" not in kwargs
        label = gw._client._record_api_latency.call_args[0][0]
        assert label == "update_order"


# ---------------------------------------------------------------------------
# Facade forwarding
# ---------------------------------------------------------------------------

class TestFacadeForwardsTimeout:
    """ShioajiClientFacade forwards timeout to OrderGateway."""

    def test_cancel_order_forwards_timeout(self) -> None:
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        with patch.object(ShioajiClientFacade, "__init__", lambda self, **kw: None):
            facade = ShioajiClientFacade.__new__(ShioajiClientFacade)
            facade.order_gateway = MagicMock()
            trade = MagicMock()
            facade.cancel_order(trade, timeout=0)
            facade.order_gateway.cancel_order.assert_called_once_with(trade, timeout=0)

    def test_update_order_forwards_timeout(self) -> None:
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        with patch.object(ShioajiClientFacade, "__init__", lambda self, **kw: None):
            facade = ShioajiClientFacade.__new__(ShioajiClientFacade)
            facade.order_gateway = MagicMock()
            trade = MagicMock()
            facade.update_order(trade, price=50.0, timeout=0)
            facade.order_gateway.update_order.assert_called_once_with(
                trade, price=50.0, qty=None, timeout=0,
            )


# ---------------------------------------------------------------------------
# BrokerCapabilities
# ---------------------------------------------------------------------------

class TestBrokerCapabilities:
    """BrokerCapabilities includes supports_nonblocking_order field."""

    def test_shioaji_supports_nonblocking(self) -> None:
        from hft_platform.broker.protocol import SHIOAJI_CAPABILITIES

        assert SHIOAJI_CAPABILITIES.supports_nonblocking_order is True

    def test_fubon_default_no_nonblocking(self) -> None:
        from hft_platform.broker.protocol import FUBON_CAPABILITIES

        assert FUBON_CAPABILITIES.supports_nonblocking_order is False

    def test_default_is_false(self) -> None:
        from hft_platform.broker.protocol import BrokerCapabilities

        caps = BrokerCapabilities(name="test")
        assert caps.supports_nonblocking_order is False
