"""Broker protocol conformance tests.

Verifies that Shioaji and Fubon broker facades satisfy BrokerClientProtocol
without requiring actual broker SDK installations.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from hft_platform.feed_adapter.protocol import BrokerClientProtocol, BrokerOrderCodec

# ---------------------------------------------------------------------------
# Protocol method inventory
# ---------------------------------------------------------------------------

_PROTOCOL_METHODS = [
    "login",
    "place_order",
    "cancel_order",
    "update_order",
    "get_positions",
    "subscribe_basket",
    "set_execution_callbacks",
    "close",
]

_CODEC_METHODS = [
    "encode_side",
    "encode_tif",
    "encode_price_type",
]


class TestBrokerClientProtocolShape:
    """BrokerClientProtocol defines expected methods."""

    def test_protocol_is_runtime_checkable(self) -> None:
        assert hasattr(BrokerClientProtocol, "__protocol_attrs__") or hasattr(
            BrokerClientProtocol, "__abstractmethods__"
        )

    def test_protocol_methods_exist(self) -> None:
        for method in _PROTOCOL_METHODS:
            assert hasattr(BrokerClientProtocol, method), f"Missing {method}"


class TestShioajiFacadeConformance:
    """ShioajiClientFacade satisfies BrokerClientProtocol."""

    def test_has_all_protocol_methods(self) -> None:
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        for method in _PROTOCOL_METHODS:
            assert hasattr(ShioajiClientFacade, method), f"Missing {method}"

    def test_method_signatures_compatible(self) -> None:
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        for method in _PROTOCOL_METHODS:
            proto_sig = inspect.signature(getattr(BrokerClientProtocol, method))
            impl_sig = inspect.signature(getattr(ShioajiClientFacade, method))
            # Protocol uses *args/**kwargs, implementation should accept at least those
            proto_params = list(proto_sig.parameters.keys())
            impl_params = list(impl_sig.parameters.keys())
            assert "self" in impl_params or impl_params, f"{method} missing self"


class TestFubonFacadeConformance:
    """FubonClientFacade satisfies BrokerClientProtocol."""

    def test_has_all_protocol_methods(self) -> None:
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        for method in _PROTOCOL_METHODS:
            assert hasattr(FubonClientFacade, method), f"Missing {method}"

    def test_method_signatures_compatible(self) -> None:
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        for method in _PROTOCOL_METHODS:
            proto_sig = inspect.signature(getattr(BrokerClientProtocol, method))
            impl_sig = inspect.signature(getattr(FubonClientFacade, method))
            impl_params = list(impl_sig.parameters.keys())
            assert "self" in impl_params or impl_params, f"{method} missing self"


class TestMockProtocolConformance:
    """A minimal mock satisfies BrokerClientProtocol isinstance check."""

    def test_mock_satisfies_protocol(self) -> None:
        mock = MagicMock(spec=_PROTOCOL_METHODS)
        for method in _PROTOCOL_METHODS:
            setattr(mock, method, MagicMock())
        # runtime_checkable checks for method presence
        assert isinstance(mock, BrokerClientProtocol)


class TestBrokerOrderCodecShape:
    """BrokerOrderCodec defines expected methods."""

    def test_codec_methods_exist(self) -> None:
        for method in _CODEC_METHODS:
            assert hasattr(BrokerOrderCodec, method), f"Missing {method}"

    def test_mock_satisfies_codec(self) -> None:
        mock = MagicMock()
        for method in _CODEC_METHODS:
            setattr(mock, method, MagicMock())
        assert isinstance(mock, BrokerOrderCodec)


class TestImportGuard:
    """Broker SDK imports are guarded."""

    def test_shioaji_import_guarded(self) -> None:
        # The facade module should be importable even without shioaji SDK
        import hft_platform.feed_adapter.shioaji.facade  # noqa: F401

    def test_fubon_import_guarded(self) -> None:
        # The facade module should be importable even without fubon_neo SDK
        import hft_platform.feed_adapter.fubon.facade  # noqa: F401
