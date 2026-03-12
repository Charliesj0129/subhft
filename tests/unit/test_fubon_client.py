"""Tests for Fubon TradeAPI client delegation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon.account import FubonAccountGateway as StubAccountGateway
from hft_platform.feed_adapter.fubon.client import FubonClient
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote import FubonQuoteRuntime as StubQuoteRuntime
from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime

# ------------------------------------------------------------------ #
# Init / properties
# ------------------------------------------------------------------ #


class TestFubonClientInit:
    def test_fubon_client_init(self) -> None:
        client = FubonClient()
        assert client.logged_in is False
        assert client.api is None

    def test_fubon_client_has_slots(self) -> None:
        assert hasattr(FubonClient, "__slots__")
        client = FubonClient()
        with pytest.raises(AttributeError):
            client.nonexistent_attr = 42  # type: ignore[attr-defined]

    def test_fubon_client_logged_in_property(self) -> None:
        client = FubonClient()
        assert client.logged_in is False

    def test_fubon_client_sub_components_initialised(self) -> None:
        client = FubonClient()
        assert client._order_gateway is not None
        assert client._account_gateway is not None
        assert client._quote_runtime is not None


# ------------------------------------------------------------------ #
# Order delegation
# ------------------------------------------------------------------ #


class TestFubonClientOrderDelegation:
    def test_place_order_delegates(self) -> None:
        client = FubonClient()
        mock_gw = MagicMock(spec=FubonOrderGateway)
        mock_gw.place_order.return_value = "ok"
        client._order_gateway = mock_gw

        result = client.place_order(symbol="2330", price=5000000, qty=1, side="Buy")
        mock_gw.place_order.assert_called_once_with(
            symbol="2330",
            price=5000000,
            qty=1,
            side="Buy",
        )
        assert result == "ok"

    def test_cancel_order_delegates(self) -> None:
        client = FubonClient()
        mock_gw = MagicMock(spec=FubonOrderGateway)
        mock_gw.cancel_order.return_value = "cancelled"
        client._order_gateway = mock_gw

        result = client.cancel_order("order-123")
        mock_gw.cancel_order.assert_called_once_with("order-123")
        assert result == "cancelled"

    def test_update_order_delegates(self) -> None:
        client = FubonClient()
        mock_gw = MagicMock(spec=FubonOrderGateway)
        mock_gw.update_order.return_value = "updated"
        client._order_gateway = mock_gw

        result = client.update_order("order-123", price=5100000, qty=2)
        mock_gw.update_order.assert_called_once_with(
            "order-123",
            price=5100000,
            qty=2,
        )
        assert result == "updated"


# ------------------------------------------------------------------ #
# Account delegation
# ------------------------------------------------------------------ #


class TestFubonClientAccountDelegation:
    def test_get_positions_delegates(self) -> None:
        client = FubonClient()
        mock_acct = MagicMock()
        mock_acct.get_inventories.return_value = [{"symbol": "2330"}]
        client._account_gateway = mock_acct

        result = client.get_positions()
        mock_acct.get_inventories.assert_called_once()
        assert result == [{"symbol": "2330"}]

    def test_get_account_balance_delegates(self) -> None:
        client = FubonClient()
        mock_acct = MagicMock()
        mock_acct.get_accounting.return_value = {"balance": 100}
        client._account_gateway = mock_acct

        result = client.get_account_balance()
        mock_acct.get_accounting.assert_called_once()
        assert result == {"balance": 100}

    def test_get_margin_delegates(self) -> None:
        client = FubonClient()
        mock_acct = MagicMock()
        mock_acct.get_margin.return_value = {"margin": 50}
        client._account_gateway = mock_acct

        result = client.get_margin()
        mock_acct.get_margin.assert_called_once()
        assert result == {"margin": 50}


# ------------------------------------------------------------------ #
# Quote delegation
# ------------------------------------------------------------------ #


class TestFubonClientQuoteDelegation:
    def test_subscribe_basket_registers_and_subscribes(self) -> None:
        client = FubonClient()
        mock_qr = MagicMock()
        client._quote_runtime = mock_qr
        client._symbols = ["2330", "2317"]

        cb = MagicMock()
        client.subscribe_basket(cb)
        mock_qr.register_quote_callbacks.assert_called_once_with(cb, cb)
        mock_qr.subscribe.assert_called_once_with(["2330", "2317"])

    def test_subscribe_basket_no_symbols(self) -> None:
        client = FubonClient()
        mock_qr = MagicMock()
        client._quote_runtime = mock_qr
        client._symbols = []

        cb = MagicMock()
        client.subscribe_basket(cb)
        mock_qr.register_quote_callbacks.assert_called_once_with(cb, cb)
        mock_qr.subscribe.assert_not_called()

    def test_close_stops_quote_runtime(self) -> None:
        client = FubonClient()
        mock_qr = MagicMock()
        client._quote_runtime = mock_qr

        client.close()
        mock_qr.stop.assert_called_once()
        assert client.logged_in is False

    def test_shutdown_delegates_to_close(self) -> None:
        client = FubonClient()
        mock_qr = MagicMock()
        client._quote_runtime = mock_qr

        client.shutdown(logout=True)
        mock_qr.stop.assert_called_once()
        assert client.logged_in is False


# ------------------------------------------------------------------ #
# Execution callbacks
# ------------------------------------------------------------------ #


class TestFubonClientCallbacks:
    def test_set_execution_callbacks_stores(self) -> None:
        client = FubonClient()
        on_order = MagicMock()
        on_deal = MagicMock()

        client.set_execution_callbacks(on_order, on_deal)
        assert client._on_order_cb is on_order
        assert client._on_deal_cb is on_deal


# ------------------------------------------------------------------ #
# Placeholders return sensible defaults
# ------------------------------------------------------------------ #


class TestFubonClientPlaceholders:
    def test_fetch_snapshots_returns_empty(self) -> None:
        client = FubonClient()
        assert client.fetch_snapshots() == []

    def test_list_profit_loss_returns_empty(self) -> None:
        client = FubonClient()
        assert client.list_profit_loss() == []

    def test_list_position_detail_returns_empty(self) -> None:
        client = FubonClient()
        assert client.list_position_detail() == []

    def test_get_exchange_returns_empty_string(self) -> None:
        client = FubonClient()
        assert client.get_exchange("2330") == ""

    def test_validate_symbols_returns_empty(self) -> None:
        client = FubonClient()
        assert client.validate_symbols() == []

    def test_get_contract_refresh_status_returns_empty_dict(self) -> None:
        client = FubonClient()
        assert client.get_contract_refresh_status() == {}

    def test_resubscribe_returns_true(self) -> None:
        client = FubonClient()
        assert client.resubscribe() is True


# ------------------------------------------------------------------ #
# Session runtime (stub fallback)
# ------------------------------------------------------------------ #


class TestFubonSessionRuntime:
    def test_fubon_session_runtime_init(self) -> None:
        runtime = FubonSessionRuntime(client=None)
        assert runtime._client is None

    def test_fubon_session_runtime_has_slots(self) -> None:
        assert hasattr(FubonSessionRuntime, "__slots__")

    def test_fubon_session_methods_raise_without_impl(self) -> None:
        """When session_runtime.py is not importable, stubs raise."""
        runtime = FubonSessionRuntime(client=None)
        # _impl is None when session_runtime module is absent
        runtime._impl = None
        with pytest.raises(NotImplementedError):
            runtime.login()
        with pytest.raises(NotImplementedError):
            runtime.refresh_token()
        with pytest.raises(NotImplementedError):
            runtime.logout()


# ------------------------------------------------------------------ #
# Legacy stub modules still importable
# ------------------------------------------------------------------ #


class TestLegacyStubImports:
    """Ensure the old stub modules (account.py, quote.py) are still importable."""

    def test_stub_account_gateway_importable(self) -> None:
        gw = StubAccountGateway(client=None)
        assert gw._client is None

    def test_stub_quote_runtime_importable(self) -> None:
        rt = StubQuoteRuntime(client=None)
        assert rt._client is None


# ------------------------------------------------------------------ #
# Order gateway (existing tests preserved)
# ------------------------------------------------------------------ #


class TestFubonOrderGateway:
    def test_fubon_order_gateway_init(self) -> None:
        gw = FubonOrderGateway(client=None)
        assert gw._client is None

    def test_fubon_order_gateway_has_slots(self) -> None:
        assert hasattr(FubonOrderGateway, "__slots__")

    def test_fubon_order_methods_raise_without_sdk(self) -> None:
        gw = FubonOrderGateway(client=None)
        with pytest.raises(NotImplementedError):
            gw.place_order()
        with pytest.raises(NotImplementedError):
            gw.cancel_order(None)
        with pytest.raises(NotImplementedError):
            gw.update_order(None)
        with pytest.raises(NotImplementedError):
            gw.batch_place_orders([])


# ------------------------------------------------------------------ #
# Account gateway (existing tests preserved)
# ------------------------------------------------------------------ #


class TestFubonAccountGateway:
    def test_fubon_account_gateway_init(self) -> None:
        gw = StubAccountGateway(client=None)
        assert gw._client is None

    def test_fubon_account_gateway_has_slots(self) -> None:
        assert hasattr(StubAccountGateway, "__slots__")

    def test_fubon_account_methods_raise(self) -> None:
        gw = StubAccountGateway(client=None)
        with pytest.raises(NotImplementedError):
            gw.get_positions()
        with pytest.raises(NotImplementedError):
            gw.get_balance()
        with pytest.raises(NotImplementedError):
            gw.get_margin()
        with pytest.raises(NotImplementedError):
            gw.list_profit_loss()
