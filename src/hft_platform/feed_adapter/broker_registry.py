"""Broker registry — protocol + register/get helpers for multi-broker support."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from structlog import get_logger

logger = get_logger("broker_registry")


@runtime_checkable
class BrokerFactory(Protocol):
    """Protocol that every broker factory must satisfy."""

    __slots__ = ()

    def create_clients(
        self,
        symbols_path: str,
        broker_config: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Return (market_data_client, order_client)."""
        ...


_registry: dict[str, BrokerFactory] = {}


def register_broker(name: str, factory: BrokerFactory) -> None:
    """Register a broker factory under *name* (idempotent for same instance)."""
    existing = _registry.get(name)
    if existing is not None and existing is not factory:
        logger.warning("broker_factory_overwritten", name=name)
    _registry[name] = factory
    logger.info("broker_factory_registered", name=name)


def get_broker_factory(name: str) -> BrokerFactory:
    """Return the registered factory for *name*, or raise KeyError."""
    try:
        return _registry[name]
    except KeyError:
        raise KeyError(
            f"No broker factory registered for {name!r}. "
            f"Available: {sorted(_registry)}"
        ) from None
