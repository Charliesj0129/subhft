"""Tests for BrokerClientProtocol and BrokerOrderCodec runtime checks."""

from __future__ import annotations

from typing import Any

import pytest

from hft_platform.feed_adapter.protocol import BrokerClientProtocol, BrokerOrderCodec

# ---------------------------------------------------------------------------
# Minimal conforming stubs
# ---------------------------------------------------------------------------

class _CompliantBrokerStub:
    """Minimal class that satisfies BrokerClientProtocol."""

    def login(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def cancel_order(self, trade: Any) -> Any:
        return None

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        return None

    def get_positions(self) -> list[Any]:
        return []

    def subscribe_basket(self, cb: Any) -> None:
        pass

    def set_execution_callbacks(self, on_order: Any, on_deal: Any) -> None:
        pass

    def close(self, logout: bool = False) -> None:
        pass


class _CompliantCodecStub:
    """Minimal class that satisfies BrokerOrderCodec."""

    def encode_side(self, side: str) -> Any:
        return side

    def encode_tif(self, tif: str) -> Any:
        return tif

    def encode_price_type(self, price_type: str) -> Any:
        return price_type


# ---------------------------------------------------------------------------
# Non-conforming stubs (missing methods)
# ---------------------------------------------------------------------------

class _MissingPlaceOrder:
    """Missing ``place_order`` — must NOT satisfy BrokerClientProtocol."""

    def login(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def cancel_order(self, trade: Any) -> Any:
        return None

    def update_order(self, trade: Any, price: float | None = None, qty: int | None = None) -> Any:
        return None

    def get_positions(self) -> list[Any]:
        return []

    def subscribe_basket(self, cb: Any) -> None:
        pass

    def set_execution_callbacks(self, on_order: Any, on_deal: Any) -> None:
        pass

    def close(self, logout: bool = False) -> None:
        pass


class _MissingEncodeSide:
    """Missing ``encode_side`` — must NOT satisfy BrokerOrderCodec."""

    def encode_tif(self, tif: str) -> Any:
        return tif

    def encode_price_type(self, price_type: str) -> Any:
        return price_type


# ---------------------------------------------------------------------------
# Tests — compliant stubs
# ---------------------------------------------------------------------------

class TestCompliantStubs:
    def test_compliant_broker_passes_isinstance(self) -> None:
        stub = _CompliantBrokerStub()
        assert isinstance(stub, BrokerClientProtocol)

    def test_compliant_codec_passes_isinstance(self) -> None:
        stub = _CompliantCodecStub()
        assert isinstance(stub, BrokerOrderCodec)


# ---------------------------------------------------------------------------
# Tests — non-conforming stubs
# ---------------------------------------------------------------------------

class TestNonConformingStubs:
    def test_missing_place_order_fails_isinstance(self) -> None:
        stub = _MissingPlaceOrder()
        assert not isinstance(stub, BrokerClientProtocol)

    def test_missing_encode_side_fails_isinstance(self) -> None:
        stub = _MissingEncodeSide()
        assert not isinstance(stub, BrokerOrderCodec)

    def test_plain_object_fails_broker_protocol(self) -> None:
        assert not isinstance(object(), BrokerClientProtocol)

    def test_plain_object_fails_codec_protocol(self) -> None:
        assert not isinstance(object(), BrokerOrderCodec)


# ---------------------------------------------------------------------------
# Tests — ShioajiClientFacade structural conformance
# ---------------------------------------------------------------------------

class TestShioajiFacadeConformance:
    """Verify the existing ShioajiClientFacade satisfies BrokerClientProtocol.

    ShioajiClientFacade requires the ``shioaji`` SDK at import time, so we
    use ``importorskip`` and fall back gracefully in CI environments that
    lack the SDK.
    """

    @pytest.fixture()
    def facade_cls(self):
        pytest.importorskip("shioaji", reason="shioaji SDK not installed")
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        return ShioajiClientFacade

    def test_facade_is_structural_subtype(self, facade_cls: type) -> None:
        # Runtime-checkable protocols use structural isinstance checks.
        # We verify at the *class* level that all required methods exist.
        required_methods = [
            "login",
            "place_order",
            "cancel_order",
            "update_order",
            "get_positions",
            "subscribe_basket",
            "set_execution_callbacks",
            "close",
        ]
        for method in required_methods:
            assert hasattr(facade_cls, method), f"ShioajiClientFacade missing {method}"
            assert callable(getattr(facade_cls, method)), f"{method} is not callable"
