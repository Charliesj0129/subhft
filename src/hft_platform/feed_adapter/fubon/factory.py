"""Fubon broker factory — creates FubonClientFacade pairs."""

from __future__ import annotations

from typing import Any

from structlog import get_logger

logger = get_logger("fubon.factory")


class FubonBrokerFactory:
    """Creates market-data and order FubonClientFacade instances.

    Satisfies the ``BrokerFactory`` protocol defined in
    ``hft_platform.feed_adapter.broker_registry``.
    """

    __slots__ = ()

    def create_clients(self, symbols_path: str, broker_config: dict[str, Any]) -> tuple[Any, Any]:
        """Return (market_data_client, order_client) for Fubon."""
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        md_client = FubonClientFacade(symbols_path, broker_config)
        order_client = FubonClientFacade(symbols_path, broker_config)
        logger.info("fubon_clients_created")
        return md_client, order_client
