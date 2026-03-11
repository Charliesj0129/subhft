"""Broker abstraction protocols for multi-broker support.

All broker client implementations (Shioaji, Fubon, etc.) must satisfy
``BrokerClientProtocol``.  Order-enum encoding is abstracted behind
``BrokerOrderCodec`` so strategy/risk layers stay broker-agnostic.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrokerClientProtocol(Protocol):
    """Protocol that all broker client facades must satisfy.

    Method signatures are derived from ``ShioajiClientFacade`` (the
    reference implementation).  New broker adapters (e.g. Fubon) must
    expose at least these entry-points.
    """

    def login(self, *args: Any, **kwargs: Any) -> bool:
        """Authenticate with the broker and return success flag."""
        ...

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        """Submit an order to the broker. Returns broker-specific receipt."""
        ...

    def cancel_order(self, trade: Any) -> Any:
        """Cancel an outstanding order identified by *trade* handle."""
        ...

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        """Modify price and/or quantity of an outstanding order."""
        ...

    def get_positions(self) -> list[Any]:
        """Return current position snapshot from the broker."""
        ...

    def subscribe_basket(self, cb: Any) -> None:
        """Subscribe to market-data for the configured symbol basket.

        *cb* is a broker-specific tick callback.
        """
        ...

    def set_execution_callbacks(self, on_order: Any, on_deal: Any) -> None:
        """Register execution-report callbacks (order updates + fills)."""
        ...

    def close(self, logout: bool = False) -> None:
        """Gracefully release broker resources."""
        ...


@runtime_checkable
class BrokerOrderCodec(Protocol):
    """Protocol for broker-specific order enum encoding.

    Translates platform-neutral string identifiers (``"Buy"``,
    ``"ROD"``, ``"LMT"``) into broker SDK constants.
    """

    def encode_side(self, side: str) -> Any:
        """Map ``"Buy"``/``"Sell"`` to broker SDK action enum."""
        ...

    def encode_tif(self, tif: str) -> Any:
        """Map time-in-force string (e.g. ``"ROD"``, ``"IOC"``) to SDK enum."""
        ...

    def encode_price_type(self, price_type: str) -> Any:
        """Map price type (e.g. ``"LMT"``, ``"MKT"``) to SDK enum."""
        ...
