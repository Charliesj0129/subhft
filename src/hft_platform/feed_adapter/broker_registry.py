"""Broker registry for multi-broker switching.

Provides a plugin-style registry where broker factories can be registered
and retrieved by name. Broker selection is controlled via the HFT_BROKER
environment variable (default: "shioaji").
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

from structlog import get_logger

logger = get_logger("broker_registry")

DEFAULT_BROKER = "shioaji"


@runtime_checkable
class BrokerFactory(Protocol):
    """Protocol for broker factory implementations.

    Each broker (Shioaji, Fubon, etc.) provides a factory that creates
    a (MarketDataProvider, OrderExecutor) client pair.
    """

    def create_clients(
        self, symbols_path: str, broker_config: dict[str, Any]
    ) -> tuple[Any, Any]: ...


_BROKER_REGISTRY: dict[str, BrokerFactory] = {}


def register_broker(name: str, factory: BrokerFactory) -> None:
    """Register a broker factory under a case-insensitive name."""
    key = name.lower()
    _BROKER_REGISTRY[key] = factory
    logger.info("broker_registered", name=key)


def get_broker_factory(name: str | None = None) -> BrokerFactory:
    """Retrieve a registered broker factory by name or HFT_BROKER env var.

    Raises ``ValueError`` if the broker name is not registered.
    """
    key = (name or os.getenv("HFT_BROKER", DEFAULT_BROKER)).lower()
    factory = _BROKER_REGISTRY.get(key)
    if factory is None:
        raise ValueError(
            f"Unknown broker {key!r}. Registered: {sorted(_BROKER_REGISTRY)}"
        )
    return factory


def list_brokers() -> list[str]:
    """Return sorted list of registered broker names."""
    return sorted(_BROKER_REGISTRY)
