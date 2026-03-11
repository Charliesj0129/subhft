"""Broker registry — dispatches broker creation by name (HFT_BROKER env var)."""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

from structlog import get_logger

logger = get_logger("broker_registry")

DEFAULT_BROKER = "shioaji"


@runtime_checkable
class BrokerFactory(Protocol):
    """Protocol that each broker factory must satisfy."""

    def create_clients(self, symbols_path: str, broker_config: dict[str, Any]) -> tuple[Any, Any]: ...


_BROKER_REGISTRY: dict[str, BrokerFactory] = {}


def register_broker(name: str, factory: BrokerFactory) -> None:
    """Register a broker factory under the given name."""
    _BROKER_REGISTRY[name.lower()] = factory
    logger.info("broker_registered", name=name.lower())


def get_broker_factory(name: str | None = None) -> BrokerFactory:
    """Look up a broker factory by name (defaults to HFT_BROKER env or 'shioaji')."""
    key = (name or os.getenv("HFT_BROKER", DEFAULT_BROKER)).lower()
    factory = _BROKER_REGISTRY.get(key)
    if factory is None:
        raise ValueError(f"Unknown broker {key!r}. Registered: {sorted(_BROKER_REGISTRY)}")
    return factory


def list_brokers() -> list[str]:
    """Return sorted list of registered broker names."""
    return sorted(_BROKER_REGISTRY)
