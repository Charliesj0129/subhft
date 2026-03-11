"""Broker-agnostic factory registry for multi-broker support.

Brokers register themselves via ``register_broker()`` and are selected
at runtime by the ``HFT_BROKER`` environment variable (default: ``shioaji``).
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

from structlog import get_logger

logger = get_logger("broker_registry")

DEFAULT_BROKER = "shioaji"


@runtime_checkable
class BrokerFactory(Protocol):
    """Protocol that each broker adapter must implement to be registerable."""

    def create_clients(
        self,
        symbols_path: str,
        broker_config: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Return (market_data_client, order_client) pair.

        Both clients must satisfy the relevant protocols from
        ``hft_platform.feed_adapter.protocols``.
        """
        ...


_BROKER_REGISTRY: dict[str, BrokerFactory] = {}


def register_broker(name: str, factory: BrokerFactory) -> None:
    """Register a broker factory under *name* (lowercase)."""
    key = name.lower()
    if key in _BROKER_REGISTRY:
        logger.warning("broker_factory_overwrite", name=key)
    _BROKER_REGISTRY[key] = factory
    logger.info("broker_factory_registered", name=key)


def get_broker_factory(name: str | None = None) -> BrokerFactory:
    """Look up a registered broker factory by *name*.

    Falls back to ``HFT_BROKER`` env var, then ``DEFAULT_BROKER``.
    Raises ``ValueError`` if the broker is not registered.
    """
    key = (name or os.getenv("HFT_BROKER", DEFAULT_BROKER)).lower()
    factory = _BROKER_REGISTRY.get(key)
    if factory is None:
        registered = sorted(_BROKER_REGISTRY)
        raise ValueError(f"Unknown broker {key!r}. Registered brokers: {registered}")
    return factory


def list_brokers() -> list[str]:
    """Return sorted list of registered broker names."""
    return sorted(_BROKER_REGISTRY)
