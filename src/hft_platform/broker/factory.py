from __future__ import annotations

import os
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

# Type for broker constructor: takes config dict, returns broker instance
BrokerConstructor = Callable[[dict[str, Any]], Any]


class BrokerFactory:
    """Registry-based factory for broker client instantiation.

    Usage:
        BrokerFactory.register("shioaji", create_shioaji_client)
        BrokerFactory.register("fubon", create_fubon_client)

        client = BrokerFactory.create("shioaji", config)
        # or
        client = BrokerFactory.create_from_env(config)  # reads HFT_BROKER
    """

    _registry: dict[str, BrokerConstructor] = {}

    @classmethod
    def register(cls, name: str, constructor: BrokerConstructor) -> None:
        """Register a broker constructor by name."""
        if name in cls._registry:
            logger.warning("broker_factory.overwrite", name=name)
        cls._registry[name] = constructor
        logger.info("broker_factory.registered", name=name)

    @classmethod
    def create(cls, name: str, config: dict[str, Any]) -> Any:
        """Create a broker client by name."""
        if name not in cls._registry:
            available = sorted(cls._registry.keys())
            raise ValueError(
                f"Unknown broker '{name}'. Available: {available}"
            )
        logger.info("broker_factory.creating", broker=name)
        return cls._registry[name](config)

    @classmethod
    def create_from_env(cls, config: dict[str, Any]) -> Any:
        """Create broker client based on HFT_BROKER env var."""
        broker_name = os.environ.get("HFT_BROKER", "shioaji")
        return cls.create(broker_name, config)

    @classmethod
    def available(cls) -> list[str]:
        """List registered broker names."""
        return sorted(cls._registry.keys())

    @classmethod
    def is_registered(cls, name: str) -> bool:
        """Check if a broker is registered."""
        return name in cls._registry

    @classmethod
    def clear(cls) -> None:
        """Clear all registrations (for testing)."""
        cls._registry.clear()


def register_broker(name: str) -> Callable[[BrokerConstructor], BrokerConstructor]:
    """Decorator to register a broker constructor."""

    def decorator(fn: BrokerConstructor) -> BrokerConstructor:
        BrokerFactory.register(name, fn)
        return fn

    return decorator
