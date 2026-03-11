"""Protocol conformance tests for FubonClientFacade against BrokerProtocol.

Verifies that ``FubonClientFacade`` structurally satisfies the
``BrokerProtocol`` runtime-checkable protocol, that every required method
exists, and that no method raises ``NotImplementedError`` in stub mode.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.unit.fubon_mock_helper import install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.broker.protocol import (  # noqa: E402
    FUBON_CAPABILITIES,
    BrokerCapabilities,
    BrokerProtocol,
)
from hft_platform.feed_adapter.fubon.facade import FubonClientFacade  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Every method/property name that BrokerProtocol declares.
_PROTOCOL_METHODS: list[str] = [
    "logged_in",
    "login",
    "reconnect",
    "close",
    "shutdown",
    "subscribe_basket",
    "fetch_snapshots",
    "reload_symbols",
    "resubscribe",
    "get_exchange",
    "set_execution_callbacks",
    "place_order",
    "cancel_order",
    "update_order",
    "get_positions",
    "get_account_balance",
    "get_margin",
    "list_position_detail",
    "list_profit_loss",
    "validate_symbols",
    "get_contract_refresh_status",
]


def _make_stub_facade() -> FubonClientFacade:
    """Create a facade in stub mode (no real SDK)."""
    return FubonClientFacade()


def _make_mocked_facade() -> tuple[FubonClientFacade, MagicMock]:
    """Create a facade with a fully-mocked FubonSDK.

    Returns the facade and the mock SDK so callers can assert on SDK calls.
    """
    mock_sdk = MagicMock()
    # Ensure sub-component constructors receive a non-None SDK.
    facade = FubonClientFacade(sdk=mock_sdk)
    return facade, mock_sdk


# ===================================================================== #
# 1. Structural Protocol Conformance
# ===================================================================== #


class TestStructuralConformance:
    """FubonClientFacade must satisfy BrokerProtocol isinstance check."""

    def test_fubon_facade_satisfies_broker_protocol(self) -> None:
        facade = _make_stub_facade()
        assert isinstance(facade, BrokerProtocol)

    def test_fubon_facade_with_mock_sdk_satisfies_broker_protocol(self) -> None:
        facade, _ = _make_mocked_facade()
        assert isinstance(facade, BrokerProtocol)


# ===================================================================== #
# 2. Method Existence
# ===================================================================== #


class TestMethodExistence:
    """Every BrokerProtocol method/property must exist on FubonClientFacade."""

    @pytest.mark.parametrize("name", _PROTOCOL_METHODS)
    def test_method_or_property_exists(self, name: str) -> None:
        facade = _make_stub_facade()
        attr = getattr(facade, name, None)
        assert attr is not None, f"FubonClientFacade is missing '{name}'"

    @pytest.mark.parametrize("name", _PROTOCOL_METHODS)
    def test_method_is_callable_or_property(self, name: str) -> None:
        """Each protocol member must be callable (method) or a property."""
        attr = getattr(FubonClientFacade, name, None)
        assert attr is not None, f"FubonClientFacade class missing '{name}'"

        if isinstance(attr, property):
            # Property — verify the getter is present.
            assert attr.fget is not None
        else:
            # Method — verify it's callable.
            assert callable(attr), f"'{name}' is not callable"


# ===================================================================== #
# 3. Full Lifecycle Test (with mocked SDK)
# ===================================================================== #


class TestFullLifecycle:
    """Full lifecycle: construct -> login -> subscribe -> order -> shutdown."""

    def test_fubon_facade_lifecycle(self) -> None:
        facade, mock_sdk = _make_mocked_facade()

        # --- login ---
        # FubonSessionRuntime uses __slots__, so replace the whole object.
        mock_session = MagicMock()
        mock_session.login.return_value = True
        facade.session_runtime = mock_session

        result = facade.login()
        mock_session.login.assert_called_once()
        assert result is True
        assert facade.logged_in is True

        # --- subscribe_basket ---
        tick_cb = MagicMock()
        facade.subscribe_basket(tick_cb)
        # quote_runtime should have registered callbacks
        assert facade.quote_runtime is not None

        # --- place_order ---
        mock_sdk.stock.place_order.return_value = {"order_id": "ORD-001"}
        order_result = facade.place_order(
            symbol="2330",
            price=5950000,  # 595.0 * 10000
            qty=1,
            side="Buy",
        )
        assert order_result == {"order_id": "ORD-001"}

        # --- cancel_order ---
        mock_sdk.stock.cancel_order.return_value = {"status": "cancelled"}
        cancel_result = facade.cancel_order("ORD-001")
        assert cancel_result == {"status": "cancelled"}

        # --- get_positions ---
        mock_sdk.stock.inventories.return_value = [{"symbol": "2330", "qty": 1}]
        positions = facade.get_positions()
        assert isinstance(positions, list)

        # --- shutdown ---
        facade.shutdown(logout=False)
        assert facade.logged_in is False

    def test_login_failure_keeps_logged_in_false(self) -> None:
        facade, _ = _make_mocked_facade()
        mock_session = MagicMock()
        mock_session.login.return_value = False
        facade.session_runtime = mock_session
        result = facade.login()
        assert result is False
        assert facade.logged_in is False

    def test_shutdown_with_logout(self) -> None:
        facade, mock_sdk = _make_mocked_facade()
        facade._logged_in = True
        mock_session = MagicMock()
        facade.session_runtime = mock_session
        facade.shutdown(logout=True)
        mock_session.logout.assert_called_once()
        assert facade.logged_in is False


# ===================================================================== #
# 4. No NotImplementedError Test
# ===================================================================== #


_METHOD_CALL_SPECS: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
    ("login", (), {}),
    ("reconnect", (), {}),
    ("close", (), {}),
    ("shutdown", (), {}),
    ("subscribe_basket", (lambda: None,), {}),
    ("fetch_snapshots", (), {}),
    ("reload_symbols", (), {}),
    ("resubscribe", (), {}),
    ("get_exchange", ("2330",), {}),
    ("set_execution_callbacks", (lambda: None, lambda: None), {}),
    ("get_positions", (), {}),
    ("get_account_balance", (), {}),
    ("get_margin", (), {}),
    ("list_position_detail", (), {}),
    ("list_profit_loss", (), {}),
    ("validate_symbols", (), {}),
    ("get_contract_refresh_status", (), {}),
]


class TestNoNotImplementedError:
    """No BrokerProtocol method should raise NotImplementedError in stub mode."""

    @pytest.mark.parametrize(
        "method,args,kwargs",
        _METHOD_CALL_SPECS,
        ids=[spec[0] for spec in _METHOD_CALL_SPECS],
    )
    def test_fubon_facade_no_not_implemented(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        facade = _make_stub_facade()
        fn = getattr(facade, method)
        # Must not raise NotImplementedError.
        try:
            fn(*args, **kwargs)
        except NotImplementedError:
            pytest.fail(f"FubonClientFacade.{method}() raised NotImplementedError")
        except Exception:
            # Other exceptions (e.g. missing SDK) are acceptable here;
            # we only care that NotImplementedError is not raised.
            pass

    def test_place_order_no_not_implemented_with_mock(self) -> None:
        """place_order delegates to order_gateway which needs an SDK."""
        facade, mock_sdk = _make_mocked_facade()
        mock_sdk.stock.place_order.return_value = {"id": "1"}
        try:
            facade.place_order(symbol="2330", price=5950000, qty=1, side="Buy")
        except NotImplementedError:
            pytest.fail("place_order raised NotImplementedError")

    def test_cancel_order_no_not_implemented_with_mock(self) -> None:
        facade, mock_sdk = _make_mocked_facade()
        mock_sdk.stock.cancel_order.return_value = {"status": "ok"}
        try:
            facade.cancel_order("ORD-001")
        except NotImplementedError:
            pytest.fail("cancel_order raised NotImplementedError")

    def test_update_order_no_not_implemented_with_mock(self) -> None:
        facade, mock_sdk = _make_mocked_facade()
        mock_sdk.stock.modify_order.return_value = {"status": "ok"}
        try:
            facade.update_order("ORD-001", price=6000000, qty=2)
        except NotImplementedError:
            pytest.fail("update_order raised NotImplementedError")


# ===================================================================== #
# 5. Capabilities Check
# ===================================================================== #


class TestCapabilities:
    """Verify FUBON_CAPABILITIES constants."""

    def test_fubon_capabilities_name(self) -> None:
        assert FUBON_CAPABILITIES.name == "fubon"

    def test_fubon_capabilities_auth_method(self) -> None:
        assert FUBON_CAPABILITIES.auth_method == "apikey"

    def test_fubon_capabilities_max_rate(self) -> None:
        assert FUBON_CAPABILITIES.max_rate_per_second == 15

    def test_fubon_capabilities_is_broker_capabilities(self) -> None:
        assert isinstance(FUBON_CAPABILITIES, BrokerCapabilities)

    def test_fubon_capabilities_supports_batch_order(self) -> None:
        assert FUBON_CAPABILITIES.supports_batch_order is True

    def test_fubon_capabilities_supports_smart_order(self) -> None:
        assert FUBON_CAPABILITIES.supports_smart_order is True

    def test_fubon_capabilities_supports_l2_depth(self) -> None:
        assert FUBON_CAPABILITIES.supports_l2_depth is True

    def test_fubon_capabilities_max_custom_field_len(self) -> None:
        assert FUBON_CAPABILITIES.max_custom_field_len == 32


# ===================================================================== #
# 6. Signature Compatibility
# ===================================================================== #


class TestSignatureCompatibility:
    """Verify method signatures are compatible with BrokerProtocol."""

    def test_close_accepts_logout_kwarg(self) -> None:
        facade = _make_stub_facade()
        facade.close(logout=True)  # must not raise

    def test_shutdown_accepts_logout_kwarg(self) -> None:
        facade = _make_stub_facade()
        facade.shutdown(logout=True)  # must not raise

    def test_update_order_accepts_price_and_qty(self) -> None:
        facade, mock_sdk = _make_mocked_facade()
        mock_sdk.stock.modify_order.return_value = {"status": "ok"}
        facade.update_order("ORD-001", price=6000000, qty=5)

    def test_list_profit_loss_accepts_dates(self) -> None:
        facade = _make_stub_facade()
        result = facade.list_profit_loss(
            account=None,
            begin_date="2026-01-01",
            end_date="2026-03-01",
        )
        assert isinstance(result, list)

    def test_get_margin_accepts_account(self) -> None:
        facade = _make_stub_facade()
        # stub mode returns None — must not raise
        facade.get_margin(account="test-account")

    def test_get_account_balance_accepts_account(self) -> None:
        facade = _make_stub_facade()
        facade.get_account_balance(account="test-account")

    def test_list_position_detail_accepts_account(self) -> None:
        facade = _make_stub_facade()
        result = facade.list_position_detail(account="test-account")
        assert isinstance(result, list)
