"""Fubon order execution gateway.

Wraps Fubon SDK (fubon_neo) order placement, cancellation, and modification.
All prices are received as scaled int (x10000) and unscaled at the SDK boundary.
"""

from __future__ import annotations

import time
from typing import Any

from structlog import get_logger

from hft_platform.feed_adapter.fubon.order_codec import FubonOrderCodec

logger = get_logger("feed_adapter.fubon.order_gateway")

PRICE_SCALE = 10000


class FubonOrderGateway:
    """Fubon order execution gateway."""

    __slots__ = ("_sdk", "_codec", "log")

    def __init__(self, sdk: Any, codec: FubonOrderCodec | None = None) -> None:
        self._sdk = sdk
        self._codec = codec or FubonOrderCodec()
        self.log = logger

    def place_order(
        self,
        symbol: str,
        price: int,
        qty: int,
        side: str,
        tif: str = "ROD",
        price_type: str = "LMT",
        order_type: str = "Stock",
    ) -> Any:
        """Place order via Fubon SDK.

        Args:
            symbol: Instrument symbol (e.g. "2330").
            price: Price as scaled int (x10000).
            qty: Order quantity.
            side: "Buy" or "Sell".
            tif: Time-in-force: "ROD", "IOC", "FOK".
            price_type: "LMT" or "MKT".
            order_type: "Stock", "DayTrade", or "Margin".

        Returns:
            SDK order result.
        """
        sdk_price = price / PRICE_SCALE
        bs_action = self._codec.encode_side(side)
        time_in_force = self._codec.encode_tif(tif)
        pt = self._codec.encode_price_type(price_type)
        ot = self._codec.encode_order_type(order_type)

        start_ns = time.perf_counter_ns()
        try:
            result = self._sdk.stock.place_order(
                buy_sell=bs_action,
                symbol=symbol,
                price=sdk_price,
                quantity=qty,
                price_type=pt,
                time_in_force=time_in_force,
                order_type=ot,
            )
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.info(
                "fubon_place_order",
                symbol=symbol,
                side=side,
                price_scaled=price,
                qty=qty,
                elapsed_us=elapsed_us,
            )
            return result
        except Exception as exc:
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.error(
                "fubon_place_order_failed",
                symbol=symbol,
                side=side,
                error=str(exc),
                elapsed_us=elapsed_us,
            )
            raise

    def place_futopt_order(
        self,
        symbol: str,
        price: int,
        qty: int,
        side: str,
        tif: str = "ROD",
        price_type: str = "LMT",
    ) -> Any:
        """Place futures/options order via Fubon SDK.

        Args:
            symbol: Futures/options symbol.
            price: Price as scaled int (x10000).
            qty: Order quantity.
            side: "Buy" or "Sell".
            tif: Time-in-force: "ROD", "IOC", "FOK".
            price_type: "LMT" or "MKT".

        Returns:
            SDK order result.
        """
        sdk_price = price / PRICE_SCALE
        bs_action = self._codec.encode_side(side)
        time_in_force = self._codec.encode_tif(tif)
        pt = self._codec.encode_price_type(price_type)

        start_ns = time.perf_counter_ns()
        try:
            result = self._sdk.futopt.place_order(
                buy_sell=bs_action,
                symbol=symbol,
                price=sdk_price,
                quantity=qty,
                price_type=pt,
                time_in_force=time_in_force,
            )
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.info(
                "fubon_place_futopt_order",
                symbol=symbol,
                side=side,
                price_scaled=price,
                qty=qty,
                elapsed_us=elapsed_us,
            )
            return result
        except Exception as exc:
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.error(
                "fubon_place_futopt_order_failed",
                symbol=symbol,
                side=side,
                error=str(exc),
                elapsed_us=elapsed_us,
            )
            raise

    def cancel_order(self, order_id: str) -> Any:
        """Cancel an existing order.

        Args:
            order_id: The order identifier to cancel.

        Returns:
            SDK cancellation result.
        """
        start_ns = time.perf_counter_ns()
        try:
            result = self._sdk.stock.cancel_order(order_id=order_id)
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.info(
                "fubon_cancel_order",
                order_id=order_id,
                elapsed_us=elapsed_us,
            )
            return result
        except Exception as exc:
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.error(
                "fubon_cancel_order_failed",
                order_id=order_id,
                error=str(exc),
                elapsed_us=elapsed_us,
            )
            raise

    def update_order(self, order_id: str, price: int, qty: int) -> Any:
        """Modify an existing order's price and/or quantity.

        Args:
            order_id: The order identifier to modify.
            price: New price as scaled int (x10000).
            qty: New quantity.

        Returns:
            SDK modification result.
        """
        sdk_price = price / PRICE_SCALE
        start_ns = time.perf_counter_ns()
        try:
            result = self._sdk.stock.modify_order(
                order_id=order_id,
                price=sdk_price,
                quantity=qty,
            )
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.info(
                "fubon_update_order",
                order_id=order_id,
                price_scaled=price,
                qty=qty,
                elapsed_us=elapsed_us,
            )
            return result
        except Exception as exc:
            elapsed_us = (time.perf_counter_ns() - start_ns) / 1000
            self.log.error(
                "fubon_update_order_failed",
                order_id=order_id,
                error=str(exc),
                elapsed_us=elapsed_us,
            )
            raise
