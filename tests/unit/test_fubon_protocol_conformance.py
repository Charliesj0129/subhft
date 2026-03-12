"""Protocol conformance tests for Fubon broker implementations.

Verifies that FubonClient (and FubonClientFacade when available) satisfy
the BrokerProtocol interface defined in ``hft_platform.broker.protocol``.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from hft_platform.broker.protocol import (
    FUBON_CAPABILITIES,
    BrokerProtocol,
)
from hft_platform.feed_adapter.fubon.client import FubonClient


def _protocol_methods() -> list[str]:
    """Derive required method names from BrokerProtocol introspection."""
    hints = typing.get_type_hints(BrokerProtocol, include_extras=True)
    members: list[str] = []
    for name in dir(BrokerProtocol):
        if name.startswith("_"):
            continue
        attr = inspect.getattr_static(BrokerProtocol, name)
        # Skip properties — they are tested separately.
        if isinstance(attr, property):
            continue
        if callable(getattr(BrokerProtocol, name)):
            members.append(name)
    return sorted(members)


PROTOCOL_METHODS: list[str] = _protocol_methods()


class TestFubonClientHasBrokerProtocolMethods:
    """Test 1: FubonClient has all BrokerProtocol methods."""

    @pytest.mark.parametrize("method_name", PROTOCOL_METHODS)
    def test_method_exists(self, method_name: str) -> None:
        assert hasattr(FubonClient, method_name), f"FubonClient missing required method: {method_name}"
        attr = getattr(FubonClient, method_name)
        assert callable(attr), f"FubonClient.{method_name} is not callable"


class TestFubonClientFacadeHasBrokerProtocolMethods:
    """Test 2: FubonClientFacade has all BrokerProtocol methods (skip if unavailable)."""

    @pytest.fixture()
    def facade_cls(self) -> type:
        mod = pytest.importorskip(
            "hft_platform.feed_adapter.fubon.facade",
            reason="FubonClientFacade not yet implemented",
        )
        return mod.FubonClientFacade

    @pytest.mark.parametrize("method_name", PROTOCOL_METHODS)
    def test_method_exists(self, facade_cls: type, method_name: str) -> None:
        assert hasattr(facade_cls, method_name), f"FubonClientFacade missing required method: {method_name}"
        attr = getattr(facade_cls, method_name)
        assert callable(attr), f"FubonClientFacade.{method_name} is not callable"


class TestFubonIsInstanceBrokerProtocol:
    """Test 3: isinstance check with runtime_checkable Protocol."""

    def test_fubon_client_isinstance(self) -> None:
        client = FubonClient()
        assert isinstance(client, BrokerProtocol), (
            "FubonClient() should satisfy BrokerProtocol via structural subtyping"
        )

    def test_fubon_client_facade_isinstance(self) -> None:
        mod = pytest.importorskip(
            "hft_platform.feed_adapter.fubon.facade",
            reason="FubonClientFacade not yet implemented",
        )
        facade = mod.FubonClientFacade()
        assert isinstance(facade, BrokerProtocol), "FubonClientFacade() should satisfy BrokerProtocol"


class TestFubonCapabilities:
    """Test 4: BrokerCapabilities for Fubon."""

    def test_name(self) -> None:
        assert FUBON_CAPABILITIES.name == "fubon"

    def test_supports_batch_order(self) -> None:
        assert FUBON_CAPABILITIES.supports_batch_order is True

    def test_supports_smart_order(self) -> None:
        assert FUBON_CAPABILITIES.supports_smart_order is True

    def test_supports_l2_depth(self) -> None:
        assert FUBON_CAPABILITIES.supports_l2_depth is True

    def test_max_custom_field_len(self) -> None:
        assert FUBON_CAPABILITIES.max_custom_field_len == 32

    def test_auth_method(self) -> None:
        assert FUBON_CAPABILITIES.auth_method == "apikey"

    def test_max_rate_per_second(self) -> None:
        assert FUBON_CAPABILITIES.max_rate_per_second == 15


class TestFubonMethodSignatureCompatibility:
    """Test 5: Method signature compatibility with BrokerProtocol."""

    @pytest.mark.parametrize("method_name", PROTOCOL_METHODS)
    def test_signature_accepts_protocol_params(self, method_name: str) -> None:
        if not hasattr(FubonClient, method_name):
            pytest.skip(f"FubonClient missing {method_name}")

        proto_sig = inspect.signature(getattr(BrokerProtocol, method_name))
        impl_sig = inspect.signature(getattr(FubonClient, method_name))

        proto_params = {k: v for k, v in proto_sig.parameters.items() if k != "self"}
        impl_params = {k: v for k, v in impl_sig.parameters.items() if k != "self"}

        # Every non-VAR parameter in the protocol should be accepted by impl
        # (impl may use *args/**kwargs to accept them).
        impl_has_var_positional = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in impl_params.values())
        impl_has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in impl_params.values())

        for param_name, param in proto_params.items():
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            if param_name in impl_params:
                continue
            if param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
                if impl_has_var_positional or impl_has_var_keyword:
                    continue
            if param.kind == inspect.Parameter.KEYWORD_ONLY:
                if impl_has_var_keyword:
                    continue

            pytest.fail(
                f"FubonClient.{method_name} does not accept parameter '{param_name}' required by BrokerProtocol"
            )


class TestFubonPropertyCheck:
    """Test 6: Verify logged_in is a property (not a method) on FubonClient."""

    def test_logged_in_is_property(self) -> None:
        assert isinstance(
            inspect.getattr_static(FubonClient, "logged_in"),
            property,
        ), "FubonClient.logged_in should be a property, not a regular method"

    def test_logged_in_returns_bool(self) -> None:
        client = FubonClient()
        result = client.logged_in
        assert isinstance(result, bool)
