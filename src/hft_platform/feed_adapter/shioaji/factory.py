"""Shioaji broker factory — registers ShioajiClientFacade with the broker registry."""
from __future__ import annotations

import os
from typing import Any

from structlog import get_logger

logger = get_logger("shioaji.factory")


class ShioajiBrokerFactory:
    """Factory that creates ShioajiClientFacade pairs for market data and order execution.

    Replicates the Shioaji-specific config logic from ``bootstrap._build_broker_clients``:
    order mode, simulation override, and CA deactivation via environment variables.
    """

    __slots__ = ()

    def create_clients(
        self,
        symbols_path: str,
        broker_config: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Create ``(market_data_client, order_client)`` using ShioajiClientFacade.

        Environment variable overrides (matching bootstrap logic):
        - ``HFT_ORDER_MODE``: ``sim|simulation|paper`` → force ``simulation=True``
        - ``HFT_ORDER_SIMULATION``: ``1|true|yes|on|sim`` → force ``simulation=True``
        - ``HFT_ORDER_NO_CA``: ``1|true|yes|on`` → ``activate_ca=False``
        - If ``simulation=True`` (from any source) → ``activate_ca=False``
        """
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

        md_cfg = dict(broker_config)
        order_cfg = dict(broker_config)

        # --- Order-side env-var overrides (exact parity with bootstrap) ---
        order_mode = os.getenv("HFT_ORDER_MODE", "").strip().lower()
        order_sim_flag = os.getenv("HFT_ORDER_SIMULATION")
        order_no_ca = os.getenv("HFT_ORDER_NO_CA", "0").lower() in {
            "1", "true", "yes", "on",
        }

        if order_mode:
            order_cfg["simulation"] = order_mode in {"sim", "simulation", "paper"}
        elif order_sim_flag is not None:
            order_cfg["simulation"] = order_sim_flag.lower() in {
                "1", "true", "yes", "on", "sim",
            }

        if order_no_ca or order_cfg.get("simulation") is True:
            order_cfg["activate_ca"] = False

        md_client = ShioajiClientFacade(symbols_path, md_cfg)
        order_client = ShioajiClientFacade(symbols_path, order_cfg)

        logger.info(
            "shioaji_clients_created",
            md_simulation=md_cfg.get("simulation"),
            order_simulation=order_cfg.get("simulation"),
        )
        return md_client, order_client
