"""Broker protocol conformance tests.

Verifies that Shioaji and Fubon broker facades satisfy BrokerClientProtocol
and BrokerOrderCodec without requiring actual broker SDK installations.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.protocol import BrokerClientProtocol, BrokerOrderCodec

# ---------------------------------------------------------------------------
# Protocol method inventory
# ---------------------------------------------------------------------------

_PROTOCOL_METHODS: list[str] = [
    "login",
    "place_order",
    "cancel_order",
    "update_order",
    "get_positions",
    "subscribe_basket",
    "set_execution_callbacks",
    "close",
]

_CODEC_METHODS: list[str] = [
    "encode_side",
    "encode_tif",
    "encode_price_type",
]


# ===================================================================
# 1. Shioaji facade satisfies BrokerClientProtocol
# ===================================================================


class TestShioajiFacadeSatisfiesProtocol:
    """ShioajiClientFacade class-level protocol conformance."""

    def test_shioaji_facade_satisfies_protocol(self) -> None:
        """isinstance check against BrokerClientProtocol on a mocked instance."""
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        # Cannot instantiate without SDK, so verify the class has all methods
        # and that a properly-specced mock passes isinstance.
        mock = MagicMock(spec=ShioajiClientFacade)
        assert isinstance(mock, BrokerClientProtocol)

    def test_shioaji_facade_has_all_protocol_methods(self) -> None:
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        for method in _PROTOCOL_METHODS:
            attr = getattr(ShioajiClientFacade, method, None)
            assert attr is not None, f"ShioajiClientFacade missing method: {method}"
            assert callable(attr), f"ShioajiClientFacade.{method} is not callable"


# ===================================================================
# 2. Fubon facade satisfies BrokerClientProtocol
# ===================================================================


class TestFubonFacadeSatisfiesProtocol:
    """FubonClientFacade class-level protocol conformance."""

    def test_fubon_facade_satisfies_protocol(self) -> None:
        """isinstance check against BrokerClientProtocol on a mocked instance."""
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        mock = MagicMock(spec=FubonClientFacade)
        assert isinstance(mock, BrokerClientProtocol)

    def test_fubon_facade_has_all_protocol_methods(self) -> None:
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        for method in _PROTOCOL_METHODS:
            attr = getattr(FubonClientFacade, method, None)
            assert attr is not None, f"FubonClientFacade missing method: {method}"
            assert callable(attr), f"FubonClientFacade.{method} is not callable"


# ===================================================================
# 3. Protocol method signatures — both facades
# ===================================================================


def _assert_signature_compatible(
    impl_cls: type,
    method: str,
    label: str,
) -> None:
    """Assert *impl_cls.method* accepts all required params of the protocol method."""
    proto_sig = inspect.signature(getattr(BrokerClientProtocol, method))
    impl_sig = inspect.signature(getattr(impl_cls, method))

    required_proto_params = {
        k
        for k, v in proto_sig.parameters.items()
        if k != "self"
        and v.default is inspect.Parameter.empty
        and v.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }

    has_var_pos = any(v.kind == inspect.Parameter.VAR_POSITIONAL for v in impl_sig.parameters.values())
    has_var_kw = any(v.kind == inspect.Parameter.VAR_KEYWORD for v in impl_sig.parameters.values())
    impl_params = set(impl_sig.parameters.keys()) - {"self"}

    for p in required_proto_params:
        assert p in impl_params or has_var_pos or has_var_kw, f"{label}.{method} missing param {p!r}"


class TestProtocolMethodSignatures:
    """Verify both facades have compatible signatures for all 8 protocol methods."""

    @pytest.mark.parametrize("method", _PROTOCOL_METHODS)
    def test_shioaji_method_callable_with_correct_params(self, method: str) -> None:
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        _assert_signature_compatible(ShioajiClientFacade, method, "ShioajiClientFacade")

    @pytest.mark.parametrize("method", _PROTOCOL_METHODS)
    def test_fubon_method_callable_with_correct_params(self, method: str) -> None:
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        _assert_signature_compatible(FubonClientFacade, method, "FubonClientFacade")


# ===================================================================
# 4. Shioaji import guard
# ===================================================================


class TestShioajiImportGuard:
    """When shioaji SDK is unavailable, the facade module still imports."""

    def test_shioaji_import_guard(self) -> None:
        # The shioaji facade module should be importable even if the shioaji
        # SDK is not installed. Verify the module can load without error.
        mod = importlib.import_module("hft_platform.feed_adapter.shioaji.facade")
        assert hasattr(mod, "ShioajiClientFacade")

    def test_shioaji_client_guards_sdk_import(self) -> None:
        """The shioaji client module should handle missing SDK gracefully."""
        # If shioaji is not installed, the client module should still be
        # importable (it uses try/except guards).
        mod = importlib.import_module("hft_platform.feed_adapter.shioaji.client")
        assert hasattr(mod, "ShioajiClient")


# ===================================================================
# 5. Fubon import guard
# ===================================================================


class TestFubonImportGuard:
    """When fubon_neo SDK is unavailable, the facade module still imports."""

    def test_fubon_import_guard(self) -> None:
        mod = importlib.import_module("hft_platform.feed_adapter.fubon.facade")
        assert hasattr(mod, "FubonClientFacade")

    def test_fubon_facade_instantiable_without_sdk(self) -> None:
        """FubonClientFacade can be instantiated when fubon_neo is absent."""
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        # fubon_neo is likely not installed in test env, so SDK will be None
        facade = FubonClientFacade(symbols_path=None, broker_config=None)
        assert facade is not None
        assert facade.logged_in is False


# ===================================================================
# 6. BrokerOrderCodec conformance
# ===================================================================


class TestBrokerOrderCodecConformance:
    """Both broker order codecs satisfy BrokerOrderCodec protocol."""

    def test_shioaji_codec_satisfies_protocol(self) -> None:
        from hft_platform.feed_adapter.shioaji.order_codec import ShioajiOrderCodec

        codec = ShioajiOrderCodec()
        assert isinstance(codec, BrokerOrderCodec)

    def test_shioaji_codec_has_all_methods(self) -> None:
        from hft_platform.feed_adapter.shioaji.order_codec import ShioajiOrderCodec

        for method in _CODEC_METHODS:
            attr = getattr(ShioajiOrderCodec, method, None)
            assert attr is not None, f"ShioajiOrderCodec missing: {method}"
            assert callable(attr), f"ShioajiOrderCodec.{method} not callable"

    def test_fubon_codec_satisfies_protocol(self) -> None:
        from hft_platform.feed_adapter.fubon.order_codec import FubonOrderCodec

        codec = FubonOrderCodec()
        assert isinstance(codec, BrokerOrderCodec)

    def test_fubon_codec_has_all_methods(self) -> None:
        from hft_platform.feed_adapter.fubon.order_codec import FubonOrderCodec

        for method in _CODEC_METHODS:
            attr = getattr(FubonOrderCodec, method, None)
            assert attr is not None, f"FubonOrderCodec missing: {method}"
            assert callable(attr), f"FubonOrderCodec.{method} not callable"


# ===================================================================
# 7. Broker registry lists both brokers
# ===================================================================


class TestBrokerRegistryListsBoth:
    """The broker registry should know about both shioaji and fubon."""

    def test_registry_has_register_function(self) -> None:
        from hft_platform.feed_adapter.broker_registry import register_broker

        assert callable(register_broker)

    def test_registry_has_list_function(self) -> None:
        from hft_platform.feed_adapter.broker_registry import list_brokers

        assert callable(list_brokers)

    def test_registry_accepts_broker_registration(self) -> None:
        """Both brokers can be registered without error."""
        from hft_platform.feed_adapter.broker_registry import (
            _BROKER_REGISTRY,
            list_brokers,
            register_broker,
        )

        # Create mock factories
        mock_shioaji_factory = MagicMock()
        mock_shioaji_factory.create_clients = MagicMock(return_value=(MagicMock(), MagicMock()))
        mock_fubon_factory = MagicMock()
        mock_fubon_factory.create_clients = MagicMock(return_value=(MagicMock(), MagicMock()))

        # Save original state and restore after test
        original = dict(_BROKER_REGISTRY)
        try:
            register_broker("shioaji", mock_shioaji_factory)
            register_broker("fubon", mock_fubon_factory)

            brokers = list_brokers()
            assert "shioaji" in brokers
            assert "fubon" in brokers
        finally:
            _BROKER_REGISTRY.clear()
            _BROKER_REGISTRY.update(original)

    def test_get_broker_factory_raises_on_unknown(self) -> None:
        from hft_platform.feed_adapter.broker_registry import get_broker_factory

        with pytest.raises(ValueError, match="Unknown broker"):
            get_broker_factory("nonexistent_broker_xyz")


# ===================================================================
# Protocol shape validation
# ===================================================================


class TestProtocolShape:
    """BrokerClientProtocol and BrokerOrderCodec are properly defined."""

    def test_protocol_is_runtime_checkable(self) -> None:
        # runtime_checkable protocols have _is_runtime_protocol attribute
        assert getattr(BrokerClientProtocol, "_is_runtime_protocol", False)

    def test_codec_is_runtime_checkable(self) -> None:
        assert getattr(BrokerOrderCodec, "_is_runtime_protocol", False)

    def test_protocol_defines_exactly_8_methods(self) -> None:
        assert len(_PROTOCOL_METHODS) == 8
        for method in _PROTOCOL_METHODS:
            assert hasattr(BrokerClientProtocol, method)

    def test_codec_defines_exactly_3_methods(self) -> None:
        assert len(_CODEC_METHODS) == 3
        for method in _CODEC_METHODS:
            assert hasattr(BrokerOrderCodec, method)

    def test_mock_satisfies_client_protocol(self) -> None:
        """A properly shaped mock passes isinstance check."""
        mock: Any = MagicMock()
        for method in _PROTOCOL_METHODS:
            setattr(mock, method, MagicMock())
        assert isinstance(mock, BrokerClientProtocol)

    def test_mock_satisfies_codec_protocol(self) -> None:
        mock: Any = MagicMock()
        for method in _CODEC_METHODS:
            setattr(mock, method, MagicMock())
        assert isinstance(mock, BrokerOrderCodec)
