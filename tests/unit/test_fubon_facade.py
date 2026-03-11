"""Unit tests for FubonClientFacade — integration layer over Fubon runtime modules."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from hft_platform.broker.protocol import BrokerProtocol
from hft_platform.feed_adapter.fubon.facade import (
    FubonClientFacade,
    _ContractsStub,
    _SubscriptionStub,
)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _make_facade(**overrides: Any) -> FubonClientFacade:
    """Create a facade with fubon_neo import stubbed out."""
    with patch.dict("sys.modules", {"fubon_neo": MagicMock(), "fubon_neo.sdk": MagicMock()}):
        facade = FubonClientFacade(
            config_path=overrides.get("config_path"),
            config=overrides.get("config"),
        )
    # Replace runtimes with mocks for delegation tests.
    for attr in (
        "session_runtime",
        "quote_runtime",
        "contracts_runtime",
        "order_gateway",
        "account_gateway",
        "subscription_manager",
    ):
        if attr not in overrides:
            setattr(facade, attr, MagicMock())
    return facade


# ---------------------------------------------------------------------- #
# Instantiation
# ---------------------------------------------------------------------- #


class TestFubonFacadeInstantiation:
    """Verify facade can be instantiated with various arg combinations."""

    def test_instantiates_with_no_args(self) -> None:
        with patch.dict("sys.modules", {"fubon_neo": MagicMock(), "fubon_neo.sdk": MagicMock()}):
            facade = FubonClientFacade()
        assert facade is not None
        assert facade._config_path is None
        assert facade._config == {}

    def test_instantiates_with_config_path_and_config(self) -> None:
        with patch.dict("sys.modules", {"fubon_neo": MagicMock(), "fubon_neo.sdk": MagicMock()}):
            facade = FubonClientFacade("/some/path.yaml", {"key": "val"})
        assert facade._config_path == "/some/path.yaml"
        assert facade._config == {"key": "val"}

    def test_instantiates_in_stub_mode_without_sdk(self) -> None:
        """When fubon_neo is not importable the facade falls back to stub mode."""
        # Ensure fubon_neo import fails
        with patch.dict("sys.modules", {"fubon_neo": None, "fubon_neo.sdk": None}):
            with patch("builtins.__import__", side_effect=_import_blocker):
                facade = FubonClientFacade()
        assert facade._sdk is None
        assert facade.logged_in is False

    def test_contracts_falls_back_to_stub(self) -> None:
        """When contracts_runtime module doesn't exist, _ContractsStub is used."""
        with patch.dict("sys.modules", {"fubon_neo": MagicMock(), "fubon_neo.sdk": MagicMock()}):
            facade = FubonClientFacade()
        assert isinstance(facade.contracts_runtime, _ContractsStub)

    def test_subscription_falls_back_to_stub(self) -> None:
        """When subscription_manager module doesn't exist, _SubscriptionStub is used."""
        with patch.dict("sys.modules", {"fubon_neo": MagicMock(), "fubon_neo.sdk": MagicMock()}):
            facade = FubonClientFacade()
        assert isinstance(facade.subscription_manager, _SubscriptionStub)


# ---------------------------------------------------------------------- #
# BrokerProtocol conformance
# ---------------------------------------------------------------------- #


class TestBrokerProtocolConformance:
    """Verify the facade satisfies BrokerProtocol at runtime."""

    def test_isinstance_broker_protocol(self) -> None:
        facade = _make_facade()
        assert isinstance(facade, BrokerProtocol)


# ---------------------------------------------------------------------- #
# Session lifecycle delegation
# ---------------------------------------------------------------------- #


class TestSessionDelegation:
    """Verify session methods delegate to session_runtime."""

    def test_login_delegates_and_sets_logged_in(self) -> None:
        facade = _make_facade()
        facade.session_runtime.login.return_value = True
        result = facade.login()
        facade.session_runtime.login.assert_called_once()
        assert result is True
        assert facade.logged_in is True

    def test_login_failure_keeps_logged_in_false(self) -> None:
        facade = _make_facade()
        facade.session_runtime.login.return_value = False
        result = facade.login()
        assert result is False
        assert facade.logged_in is False

    def test_reconnect_delegates_to_session_login(self) -> None:
        facade = _make_facade()
        facade.session_runtime.login.return_value = True
        result = facade.reconnect()
        assert result is True
        assert facade.logged_in is True

    def test_close_stops_quote_and_optionally_logs_out(self) -> None:
        facade = _make_facade()
        facade._logged_in = True
        facade.close(logout=True)
        facade.quote_runtime.stop.assert_called_once()
        facade.session_runtime.logout.assert_called_once()
        assert facade.logged_in is False

    def test_close_without_logout(self) -> None:
        facade = _make_facade()
        facade._logged_in = True
        facade.close(logout=False)
        facade.quote_runtime.stop.assert_called_once()
        facade.session_runtime.logout.assert_not_called()
        assert facade.logged_in is False

    def test_shutdown_delegates_to_close(self) -> None:
        facade = _make_facade()
        facade._logged_in = True
        facade.shutdown(logout=True)
        facade.quote_runtime.stop.assert_called_once()
        facade.session_runtime.logout.assert_called_once()
        assert facade.logged_in is False


# ---------------------------------------------------------------------- #
# Market data delegation
# ---------------------------------------------------------------------- #


class TestMarketDataDelegation:
    """Verify market data methods delegate correctly."""

    def test_subscribe_basket(self) -> None:
        facade = _make_facade()
        cb = MagicMock()
        facade.subscribe_basket(cb)
        facade.subscription_manager.subscribe_basket.assert_called_once_with(cb)

    def test_fetch_snapshots_returns_empty(self) -> None:
        facade = _make_facade()
        assert facade.fetch_snapshots() == []

    def test_reload_symbols(self) -> None:
        facade = _make_facade()
        facade.reload_symbols()
        facade.contracts_runtime.reload_symbols.assert_called_once()

    def test_resubscribe(self) -> None:
        facade = _make_facade()
        facade.subscription_manager.resubscribe.return_value = True
        assert facade.resubscribe() is True

    def test_get_exchange(self) -> None:
        facade = _make_facade()
        facade.contracts_runtime.get_exchange.return_value = "TSE"
        assert facade.get_exchange("2330") == "TSE"
        facade.contracts_runtime.get_exchange.assert_called_once_with("2330")

    def test_set_execution_callbacks(self) -> None:
        facade = _make_facade()
        on_order = MagicMock()
        on_deal = MagicMock()
        facade.set_execution_callbacks(on_order, on_deal)
        facade.subscription_manager.set_execution_callbacks.assert_called_once_with(
            on_order,
            on_deal,
        )


# ---------------------------------------------------------------------- #
# Order delegation
# ---------------------------------------------------------------------- #


class TestOrderDelegation:
    """Verify order methods delegate to order_gateway."""

    def test_place_order(self) -> None:
        facade = _make_facade()
        facade.order_gateway.place_order.return_value = {"order_id": "123"}
        result = facade.place_order(symbol="2330", price=5000000, qty=1, side="Buy")
        facade.order_gateway.place_order.assert_called_once_with(
            symbol="2330",
            price=5000000,
            qty=1,
            side="Buy",
        )
        assert result == {"order_id": "123"}

    def test_cancel_order(self) -> None:
        facade = _make_facade()
        facade.order_gateway.cancel_order.return_value = "ok"
        result = facade.cancel_order("order-abc")
        facade.order_gateway.cancel_order.assert_called_once_with("order-abc")
        assert result == "ok"

    def test_update_order(self) -> None:
        facade = _make_facade()
        facade.order_gateway.update_order.return_value = "updated"
        result = facade.update_order("order-abc", price=5100000, qty=2)
        facade.order_gateway.update_order.assert_called_once_with(
            "order-abc",
            price=5100000,
            qty=2,
        )
        assert result == "updated"


# ---------------------------------------------------------------------- #
# Account delegation
# ---------------------------------------------------------------------- #


class TestAccountDelegation:
    """Verify account methods delegate to account_gateway."""

    def test_get_positions(self) -> None:
        facade = _make_facade()
        facade.account_gateway.get_inventories.return_value = [{"sym": "2330"}]
        result = facade.get_positions()
        facade.account_gateway.get_inventories.assert_called_once()
        assert result == [{"sym": "2330"}]

    def test_get_account_balance(self) -> None:
        facade = _make_facade()
        facade.account_gateway.get_accounting.return_value = {"balance": 1000}
        result = facade.get_account_balance()
        facade.account_gateway.get_accounting.assert_called_once()
        assert result == {"balance": 1000}

    def test_get_margin(self) -> None:
        facade = _make_facade()
        facade.account_gateway.get_margin.return_value = {"margin": 500}
        result = facade.get_margin()
        facade.account_gateway.get_margin.assert_called_once()
        assert result == {"margin": 500}

    def test_list_position_detail_returns_empty(self) -> None:
        facade = _make_facade()
        assert facade.list_position_detail() == []

    def test_list_profit_loss_returns_empty(self) -> None:
        facade = _make_facade()
        assert facade.list_profit_loss() == []


# ---------------------------------------------------------------------- #
# Symbols delegation
# ---------------------------------------------------------------------- #


class TestSymbolsDelegation:
    """Verify symbol methods delegate to contracts_runtime."""

    def test_validate_symbols(self) -> None:
        facade = _make_facade()
        facade.contracts_runtime.validate_symbols.return_value = ["2330", "2317"]
        result = facade.validate_symbols()
        assert result == ["2330", "2317"]

    def test_get_contract_refresh_status(self) -> None:
        facade = _make_facade()
        facade.contracts_runtime.refresh_status.return_value = {"status": "ok"}
        result = facade.get_contract_refresh_status()
        assert result == {"status": "ok"}


# ---------------------------------------------------------------------- #
# Stub tests
# ---------------------------------------------------------------------- #


class TestInlineStubs:
    """Verify inline fallback stubs behave correctly."""

    def test_contracts_stub_defaults(self) -> None:
        stub = _ContractsStub()
        assert stub.symbols == []
        assert stub.validate_symbols() == []
        assert stub.get_exchange("2330") == ""
        assert stub.refresh_status() == {"status": "stub"}
        stub.reload_symbols()  # no-op, no error

    def test_subscription_stub_defaults(self) -> None:
        qr = MagicMock()
        stub = _SubscriptionStub(qr)
        stub.subscribe_basket(MagicMock())  # no-op
        assert stub.resubscribe() is False
        stub.set_execution_callbacks(MagicMock(), MagicMock())  # no-op


# ---------------------------------------------------------------------- #
# Import blocker helper
# ---------------------------------------------------------------------- #

_original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__  # type: ignore[attr-defined]


def _import_blocker(name: str, *args: Any, **kwargs: Any) -> Any:
    """Block fubon_neo imports to test stub mode."""
    if name.startswith("fubon_neo"):
        raise ImportError(f"Mocked ImportError for {name}")
    return _original_import(name, *args, **kwargs)
