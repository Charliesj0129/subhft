"""Tests for FubonClientFacade."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _make_logged_in_facade() -> tuple[FubonClientFacade, MagicMock]:
    """Create a facade with mocked sub-components (simulates post-login state)."""
    facade = FubonClientFacade(broker_config={"test": True})

    # Inject mocked sub-components as if login() had run.
    facade.session_runtime = MagicMock()
    facade.quote_runtime = MagicMock()
    facade.contracts_runtime = MagicMock()
    facade.order_gateway = MagicMock()
    facade.account_gateway = MagicMock()
    facade._logged_in = True

    sdk = MagicMock()
    facade._sdk = sdk
    return facade, sdk


# ---------------------------------------------------------------------- #
# Construction
# ---------------------------------------------------------------------- #


class TestFubonFacadeConstruction:
    def test_init_defaults(self) -> None:
        facade = FubonClientFacade()
        assert facade.logged_in is False
        assert facade.sdk is None
        assert facade.session_runtime is None
        assert facade.quote_runtime is None
        assert facade.order_gateway is None
        assert facade.account_gateway is None
        assert facade.contracts_runtime is None

    def test_init_with_config_path(self) -> None:
        facade = FubonClientFacade(config_path="/etc/fubon.yaml")
        assert facade._config["config_path"] == "/etc/fubon.yaml"

    def test_init_with_broker_config(self) -> None:
        cfg = {"api_key": "test123"}
        facade = FubonClientFacade(broker_config=cfg)
        assert facade._config is cfg


# ---------------------------------------------------------------------- #
# Login
# ---------------------------------------------------------------------- #


class TestFubonFacadeLogin:
    def test_login_delegates_to_session_runtime(self) -> None:
        """login() should create SDK, wire sub-components, delegate to session runtime."""
        mock_sdk_instance = MagicMock()
        mock_sdk_class = MagicMock(return_value=mock_sdk_instance)
        mock_session = MagicMock()
        mock_session.login.return_value = True

        mock_module = MagicMock()
        mock_module.FubonSDK = mock_sdk_class

        facade = FubonClientFacade()

        with (
            patch.dict("sys.modules", {"fubon_neo": mock_module}),
            patch(
                "hft_platform.feed_adapter.fubon.facade.FubonSessionRuntime",
                return_value=mock_session,
            ),
        ):
            result = facade.login()

        assert result is True
        assert facade.logged_in is True
        mock_session.login.assert_called_once()
        assert facade.session_runtime is mock_session

    def test_login_returns_false_when_sdk_missing(self) -> None:
        """login() should return False when fubon_neo is not installed."""
        facade = FubonClientFacade()

        import builtins

        original_import = builtins.__import__

        def mock_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "fubon_neo":
                raise ImportError("No module named 'fubon_neo'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = facade.login()

        assert result is False
        assert facade.logged_in is False

    def test_login_returns_false_on_session_failure(self) -> None:
        """login() should return False if session runtime login fails."""
        mock_sdk_instance = MagicMock()
        mock_sdk_class = MagicMock(return_value=mock_sdk_instance)
        mock_session = MagicMock()
        mock_session.login.return_value = False

        mock_module = MagicMock()
        mock_module.FubonSDK = mock_sdk_class

        facade = FubonClientFacade()

        with (
            patch.dict("sys.modules", {"fubon_neo": mock_module}),
            patch(
                "hft_platform.feed_adapter.fubon.facade.FubonSessionRuntime",
                return_value=mock_session,
            ),
        ):
            result = facade.login()

        assert result is False
        assert facade.logged_in is False

    def test_login_returns_false_on_exception(self) -> None:
        """login() should catch exceptions and return False."""
        mock_module = MagicMock()
        mock_module.FubonSDK.side_effect = RuntimeError("SDK init failed")

        facade = FubonClientFacade()

        with patch.dict("sys.modules", {"fubon_neo": mock_module}):
            result = facade.login()

        assert result is False
        assert facade.logged_in is False


# ---------------------------------------------------------------------- #
# Order delegation
# ---------------------------------------------------------------------- #


class TestFubonFacadeOrders:
    def test_place_order_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        contract = MagicMock()
        order = MagicMock()
        facade.order_gateway.place_order.return_value = "ORDER_ACK"

        result = facade.place_order(contract, order)

        assert result == "ORDER_ACK"
        facade.order_gateway.place_order.assert_called_once_with(contract, order)

    def test_cancel_order_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.cancel_order("ORD-123")
        facade.order_gateway.cancel_order.assert_called_once_with("ORD-123")

    def test_update_order_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.update_order("ORD-123", price=100_0000, qty=10)
        facade.order_gateway.update_order.assert_called_once_with("ORD-123", price=100_0000, qty=10)

    def test_order_before_login_raises(self) -> None:
        facade = FubonClientFacade()
        with pytest.raises(RuntimeError, match="before login"):
            facade.place_order(MagicMock(), MagicMock())
        with pytest.raises(RuntimeError, match="before login"):
            facade.cancel_order("ORD-1")
        with pytest.raises(RuntimeError, match="before login"):
            facade.update_order("ORD-1", price=100, qty=1)


# ---------------------------------------------------------------------- #
# Market data delegation
# ---------------------------------------------------------------------- #


class TestFubonFacadeMarketData:
    def test_subscribe_basket_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.subscribe_basket(["2330", "2317"])
        facade.quote_runtime.subscribe.assert_called_once_with(["2330", "2317"])

    def test_unsubscribe_basket_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.unsubscribe_basket(["2330"])
        facade.quote_runtime.unsubscribe.assert_called_once_with(["2330"])

    def test_subscribe_before_login_raises(self) -> None:
        facade = FubonClientFacade()
        with pytest.raises(RuntimeError, match="before login"):
            facade.subscribe_basket(["2330"])
        with pytest.raises(RuntimeError, match="before login"):
            facade.unsubscribe_basket(["2330"])

    def test_set_on_tick_registers_callback(self) -> None:
        facade, _ = _make_logged_in_facade()
        cb = MagicMock()
        facade.set_on_tick(cb)
        facade.quote_runtime.register_quote_callbacks.assert_called_once()
        call_kwargs = facade.quote_runtime.register_quote_callbacks.call_args
        assert call_kwargs.kwargs["on_tick"] is cb

    def test_set_on_bidask_registers_callback(self) -> None:
        facade, _ = _make_logged_in_facade()
        cb = MagicMock()
        facade.set_on_bidask(cb)
        facade.quote_runtime.register_quote_callbacks.assert_called_once()
        call_kwargs = facade.quote_runtime.register_quote_callbacks.call_args
        assert call_kwargs.kwargs["on_bidask"] is cb

    def test_set_callbacks_before_login_raises(self) -> None:
        facade = FubonClientFacade()
        with pytest.raises(RuntimeError, match="before login"):
            facade.set_on_tick(MagicMock())
        with pytest.raises(RuntimeError, match="before login"):
            facade.set_on_bidask(MagicMock())

    def test_set_tick_preserves_existing_bidask(self) -> None:
        """When setting on_tick, the existing on_bidask should be preserved."""
        facade, _ = _make_logged_in_facade()
        bidask_cb = MagicMock()
        tick_cb = MagicMock()
        facade.set_on_bidask(bidask_cb)
        facade.set_on_tick(tick_cb)
        # Second call should pass the previously-set bidask callback
        last_call = facade.quote_runtime.register_quote_callbacks.call_args
        assert last_call.kwargs["on_tick"] is tick_cb
        assert last_call.kwargs["on_bidask"] is bidask_cb


# ---------------------------------------------------------------------- #
# Account queries
# ---------------------------------------------------------------------- #


class TestFubonFacadeAccount:
    def test_get_positions_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.account_gateway.get_positions.return_value = [{"qty": 10}]
        result = facade.get_positions()
        assert result == [{"qty": 10}]
        facade.account_gateway.get_positions.assert_called_once()

    def test_get_account_balance_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.get_account_balance(account="ACC-1")
        facade.account_gateway.get_account_balance.assert_called_once_with(account="ACC-1")

    def test_get_margin_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.get_margin()
        facade.account_gateway.get_margin.assert_called_once_with(account=None)

    def test_account_before_login_raises(self) -> None:
        facade = FubonClientFacade()
        with pytest.raises(RuntimeError, match="before login"):
            facade.get_positions()
        with pytest.raises(RuntimeError, match="before login"):
            facade.get_account_balance()
        with pytest.raises(RuntimeError, match="before login"):
            facade.get_margin()


# ---------------------------------------------------------------------- #
# Contracts
# ---------------------------------------------------------------------- #


class TestFubonFacadeContracts:
    def test_validate_symbols_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.contracts_runtime.validate_symbols.return_value = ["2330"]
        result = facade.validate_symbols()
        assert result == ["2330"]

    def test_get_contract_refresh_status_delegates(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.contracts_runtime.refresh_status.return_value = {"ok": True}
        result = facade.get_contract_refresh_status()
        assert result == {"ok": True}

    def test_contracts_before_login_raises(self) -> None:
        facade = FubonClientFacade()
        with pytest.raises(RuntimeError, match="before login"):
            facade.validate_symbols()
        with pytest.raises(RuntimeError, match="before login"):
            facade.get_contract_refresh_status()


# ---------------------------------------------------------------------- #
# Logout / Shutdown
# ---------------------------------------------------------------------- #


class TestFubonFacadeShutdown:
    def test_logout_delegates_to_runtimes(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.logout()
        facade.quote_runtime.stop.assert_called_once()
        facade.session_runtime.logout.assert_called_once()
        assert facade.logged_in is False

    def test_close_without_logout(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.close(logout=False)
        facade.quote_runtime.stop.assert_called_once()
        facade.session_runtime.logout.assert_not_called()

    def test_close_with_logout(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.close(logout=True)
        facade.session_runtime.logout.assert_called_once()

    def test_shutdown_is_alias_for_close(self) -> None:
        facade, _ = _make_logged_in_facade()
        facade.shutdown(logout=True)
        facade.session_runtime.logout.assert_called_once()

    def test_logout_before_login_is_safe(self) -> None:
        """Logout on a fresh facade should not raise."""
        facade = FubonClientFacade()
        facade.logout()  # should not raise
        assert facade.logged_in is False

    def test_close_before_login_is_safe(self) -> None:
        facade = FubonClientFacade()
        facade.close()  # should not raise


# ---------------------------------------------------------------------- #
# Misc
# ---------------------------------------------------------------------- #


class TestFubonFacadeMisc:
    def test_fetch_snapshots_returns_empty(self) -> None:
        facade, _ = _make_logged_in_facade()
        result = facade.fetch_snapshots()
        assert result == []
