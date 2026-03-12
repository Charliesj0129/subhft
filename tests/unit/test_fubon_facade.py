"""Tests for FubonClientFacade — BrokerProtocol delegation layer."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.broker.protocol import BrokerProtocol
from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


@pytest.fixture()
def facade() -> FubonClientFacade:
    """Create a FubonClientFacade with None SDK (stub mode)."""
    return FubonClientFacade(symbols_path=None, broker_config=None)


@pytest.fixture()
def wired_facade() -> FubonClientFacade:
    """Create a facade with mocked sub-components for delegation tests."""
    f = FubonClientFacade(symbols_path="/tmp/symbols.yaml", broker_config={"simulation": True})
    f._session_runtime = MagicMock()
    f._quote_runtime = MagicMock()
    f._order_gateway = MagicMock()
    f._account_gateway = MagicMock()
    f._contracts_runtime = MagicMock()
    f._subscription_manager = MagicMock()
    return f


# ---------------------------------------------------------------------- #
# Protocol conformance
# ---------------------------------------------------------------------- #


class TestProtocolConformance:
    def test_isinstance_broker_protocol(self, wired_facade: FubonClientFacade) -> None:
        """FubonClientFacade must satisfy runtime_checkable BrokerProtocol."""
        assert isinstance(wired_facade, BrokerProtocol)

    def test_has_slots(self) -> None:
        assert hasattr(FubonClientFacade, "__slots__")


# ---------------------------------------------------------------------- #
# Constructor
# ---------------------------------------------------------------------- #


class TestConstructor:
    def test_constructor_none_sdk(self, facade: FubonClientFacade) -> None:
        """Facade should work even when fubon_neo SDK is not installed."""
        assert facade._sdk is None
        assert facade._symbols_path is None
        assert facade._broker_config == {}

    def test_constructor_with_config(self) -> None:
        cfg = {"simulation": True, "api_key": "test"}
        f = FubonClientFacade(symbols_path="/tmp/s.yaml", broker_config=cfg)
        assert f._symbols_path == "/tmp/s.yaml"
        assert f._broker_config == cfg


# ---------------------------------------------------------------------- #
# Session lifecycle delegation
# ---------------------------------------------------------------------- #


class TestSessionDelegation:
    def test_logged_in_delegates(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._session_runtime.is_logged_in = True
        assert wired_facade.logged_in is True

        wired_facade._session_runtime.is_logged_in = False
        assert wired_facade.logged_in is False

    def test_login_delegates(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._session_runtime.login_with_retry.return_value = True
        result = wired_facade.login()
        wired_facade._session_runtime.login_with_retry.assert_called_once()
        assert result is True

    def test_reconnect_delegates(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._session_runtime.reconnect.return_value = True
        result = wired_facade.reconnect(reason="test", force=True)
        wired_facade._session_runtime.reconnect.assert_called_once_with(reason="test", force=True)
        assert result is True


# ---------------------------------------------------------------------- #
# Close / shutdown teardown
# ---------------------------------------------------------------------- #


class TestTeardown:
    def test_close_stops_quote_runtime(self, wired_facade: FubonClientFacade) -> None:
        wired_facade.close(logout=False)
        wired_facade._quote_runtime.stop.assert_called_once()
        wired_facade._session_runtime.logout.assert_not_called()

    def test_close_with_logout(self, wired_facade: FubonClientFacade) -> None:
        wired_facade.close(logout=True)
        wired_facade._quote_runtime.stop.assert_called_once()
        wired_facade._session_runtime.logout.assert_called_once()

    def test_shutdown_delegates_to_close(self, wired_facade: FubonClientFacade) -> None:
        wired_facade.shutdown(logout=True)
        wired_facade._quote_runtime.stop.assert_called_once()
        wired_facade._session_runtime.logout.assert_called_once()


# ---------------------------------------------------------------------- #
# Market data delegation
# ---------------------------------------------------------------------- #


class TestMarketDataDelegation:
    def test_subscribe_basket(self, wired_facade: FubonClientFacade) -> None:
        cb = MagicMock()
        wired_facade.subscribe_basket(cb)
        wired_facade._subscription_manager.subscribe_basket.assert_called_once_with(cb)

    def test_fetch_snapshots_returns_empty(self, wired_facade: FubonClientFacade) -> None:
        result = wired_facade.fetch_snapshots()
        assert result == []

    def test_reload_symbols(self, wired_facade: FubonClientFacade) -> None:
        wired_facade.reload_symbols()
        wired_facade._contracts_runtime.reload_symbols.assert_called_once()

    def test_resubscribe(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._subscription_manager.resubscribe.return_value = True
        result = wired_facade.resubscribe()
        wired_facade._subscription_manager.resubscribe.assert_called_once()
        assert result is True

    def test_get_exchange(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._contracts_runtime.get_exchange.return_value = "TWSE"
        result = wired_facade.get_exchange("2330")
        wired_facade._contracts_runtime.get_exchange.assert_called_once_with("2330")
        assert result == "TWSE"

    def test_set_execution_callbacks(self, wired_facade: FubonClientFacade) -> None:
        on_order = MagicMock()
        on_deal = MagicMock()
        wired_facade.set_execution_callbacks(on_order, on_deal)
        wired_facade._subscription_manager.set_execution_callbacks.assert_called_once_with(on_order, on_deal)


# ---------------------------------------------------------------------- #
# Order delegation
# ---------------------------------------------------------------------- #


class TestOrderDelegation:
    def test_place_order(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._order_gateway.place_order.return_value = "order-123"
        result = wired_facade.place_order(symbol="2330", price=5000000, qty=1, side="Buy")
        wired_facade._order_gateway.place_order.assert_called_once_with(symbol="2330", price=5000000, qty=1, side="Buy")
        assert result == "order-123"

    def test_cancel_order(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._order_gateway.cancel_order.return_value = "cancelled"
        result = wired_facade.cancel_order("trade-obj")
        wired_facade._order_gateway.cancel_order.assert_called_once_with("trade-obj")
        assert result == "cancelled"

    def test_update_order(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._order_gateway.update_order.return_value = "updated"
        result = wired_facade.update_order("trade-obj", price=5010000, qty=2)
        wired_facade._order_gateway.update_order.assert_called_once_with("trade-obj", price=5010000, qty=2)
        assert result == "updated"


# ---------------------------------------------------------------------- #
# Account delegation
# ---------------------------------------------------------------------- #


class TestAccountDelegation:
    def test_get_positions(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._account_gateway.get_inventories.return_value = [{"sym": "2330"}]
        result = wired_facade.get_positions()
        wired_facade._account_gateway.get_inventories.assert_called_once()
        assert result == [{"sym": "2330"}]

    def test_get_account_balance(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._account_gateway.get_accounting.return_value = {"balance": 100}
        result = wired_facade.get_account_balance()
        wired_facade._account_gateway.get_accounting.assert_called_once()
        assert result == {"balance": 100}

    def test_get_margin(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._account_gateway.get_margin.return_value = {"margin": 50}
        result = wired_facade.get_margin()
        wired_facade._account_gateway.get_margin.assert_called_once()
        assert result == {"margin": 50}

    def test_list_position_detail_returns_empty(self, wired_facade: FubonClientFacade) -> None:
        result = wired_facade.list_position_detail()
        assert result == []

    def test_list_profit_loss_returns_empty(self, wired_facade: FubonClientFacade) -> None:
        result = wired_facade.list_profit_loss()
        assert result == []


# ---------------------------------------------------------------------- #
# Symbols / contracts delegation
# ---------------------------------------------------------------------- #


class TestContractsDelegation:
    def test_validate_symbols(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._contracts_runtime.validate_symbols.return_value = ["2330", "2317"]
        result = wired_facade.validate_symbols()
        wired_facade._contracts_runtime.validate_symbols.assert_called_once()
        assert result == ["2330", "2317"]

    def test_get_contract_refresh_status(self, wired_facade: FubonClientFacade) -> None:
        wired_facade._contracts_runtime.refresh_status.return_value = {"refreshed": True}
        result = wired_facade.get_contract_refresh_status()
        wired_facade._contracts_runtime.refresh_status.assert_called_once()
        assert result == {"refreshed": True}


# ---------------------------------------------------------------------- #
# Stub mode (no SDK)
# ---------------------------------------------------------------------- #


class TestStubMode:
    def test_stub_logged_in_false(self, facade: FubonClientFacade) -> None:
        """With stub session runtime, logged_in should be False."""
        assert facade.logged_in is False

    def test_stub_login_returns_false(self, facade: FubonClientFacade) -> None:
        """With stub session, login should return False (no SDK)."""
        # The real FubonSessionRuntime raises NotImplementedError;
        # the inline stub returns False.
        # We accept either behavior depending on which import succeeded.
        try:
            result = facade.login()
            assert result is False
        except NotImplementedError:
            pass  # Real stub raises — acceptable

    def test_stub_close_no_error(self, facade: FubonClientFacade) -> None:
        """close() should not raise even in stub mode."""
        facade.close(logout=False)

    def test_stub_shutdown_no_error(self, facade: FubonClientFacade) -> None:
        """shutdown() should not raise even in stub mode."""
        facade.shutdown(logout=False)

    def test_stub_fetch_snapshots_empty(self, facade: FubonClientFacade) -> None:
        assert facade.fetch_snapshots() == []

    def test_stub_list_position_detail_empty(self, facade: FubonClientFacade) -> None:
        assert facade.list_position_detail() == []

    def test_stub_list_profit_loss_empty(self, facade: FubonClientFacade) -> None:
        assert facade.list_profit_loss() == []
