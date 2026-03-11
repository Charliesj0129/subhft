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

    __slots__ = ("_sdk", "_client", "_codec", "log")

    def __init__(
        self,
        sdk: Any | None = None,
        codec: FubonOrderCodec | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        if sdk is None:
            sdk = client
        self._sdk = sdk
        self._client = sdk
        self._codec = codec or FubonOrderCodec()
        self.log = logger

    def _require_sdk(self, op: str) -> Any:
        if self._sdk is None:
            raise NotImplementedError(f"FubonOrderGateway.{op} not yet implemented")
        return self._sdk

    def place_order(
        self,
        symbol: str | None = None,
        price: int | None = None,
        qty: int | None = None,
        side: str | None = None,
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
        sdk = self._require_sdk("place_order")
        if symbol is None or price is None or qty is None or side is None:
            raise TypeError("symbol, price, qty, and side are required")
        sdk_price = price / PRICE_SCALE
        bs_action = self._codec.encode_side(side)
        time_in_force = self._codec.encode_tif(tif)
        pt = self._codec.encode_price_type(price_type)
        ot = self._codec.encode_order_type(order_type)

        start_ns = time.perf_counter_ns()
        try:
            result = sdk.stock.place_order(
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
        symbol: str | None = None,
        price: int | None = None,
        qty: int | None = None,
        side: str | None = None,
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
        sdk = self._require_sdk("place_futopt_order")
        if symbol is None or price is None or qty is None or side is None:
            raise TypeError("symbol, price, qty, and side are required")
        sdk_price = price / PRICE_SCALE
        bs_action = self._codec.encode_side(side)
        time_in_force = self._codec.encode_tif(tif)
        pt = self._codec.encode_price_type(price_type)

        start_ns = time.perf_counter_ns()
        try:
            result = sdk.futopt.place_order(
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

    @staticmethod
    def _extract_order_id(trade: Any, op: str) -> str:
        """Extract order_id from a trade object or raw string.

        Args:
            trade: Either a string order_id or an object with an ``order_id`` attribute.
            op: Operation name for error messages.

        Returns:
            The resolved order_id string.

        Raises:
            TypeError: If order_id cannot be resolved.
        """
        if isinstance(trade, str):
            return trade
        order_id = getattr(trade, "order_id", None)
        if order_id is None:
            raise TypeError(
                f"{op}: cannot extract order_id from trade "
                f"(type={type(trade).__name__})"
            )
        return order_id

    def cancel_order(self, trade: Any) -> Any:
        """Cancel an existing order.

        Accepts either a raw ``order_id`` string (backward compat) or a trade
        object with an ``order_id`` attribute (BrokerProtocol compatible).

        Args:
            trade: A string order_id or trade object with ``order_id`` attribute.

        Returns:
            SDK cancellation result.
        """
        order_id = self._extract_order_id(trade, "cancel_order")
        sdk = self._require_sdk("cancel_order")
        start_ns = time.perf_counter_ns()
        try:
            result = sdk.stock.cancel_order(order_id=order_id)
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

    def update_order(
        self,
        trade: Any,
        price: int | None = None,
        qty: int | None = None,
    ) -> Any:
        """Modify an existing order's price and/or quantity.

        Accepts either a raw ``order_id`` string (backward compat) or a trade
        object with an ``order_id`` attribute (BrokerProtocol compatible).

        Args:
            trade: A string order_id or trade object with ``order_id`` attribute.
            price: New price as scaled int (x10000).
            qty: New quantity.

        Returns:
            SDK modification result, or None if no changes requested.
        """
        if price is None and qty is None:
            self.log.warning("update_order: no price or qty provided, no-op")
            return None
        order_id = self._extract_order_id(trade, "update_order")
        sdk = self._require_sdk("update_order")
        sdk_price = price / PRICE_SCALE if price is not None else None
        modify_kwargs: dict[str, Any] = {"order_id": order_id}
        if sdk_price is not None:
            modify_kwargs["price"] = sdk_price
        if qty is not None:
            modify_kwargs["quantity"] = qty
        start_ns = time.perf_counter_ns()
        try:
            result = sdk.stock.modify_order(**modify_kwargs)
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

    def batch_place_orders(self, orders: list[dict[str, Any]]) -> list[Any]:
        """Place multiple orders sequentially with per-order error isolation.

        Fubon SDK has no native batch API, so orders are placed one at a time
        with rate-limit-aware delays (67ms = 1/15s between orders).

        Args:
            orders: List of order dicts, each with keys:
                symbol (str), price (int, scaled x10000), qty (int),
                side (str), tif (str, default "ROD"),
                price_type (str, default "LMT"),
                order_type (str, default "Stock").

        Returns:
            List of SDK results (or None for failed orders).
        """
        self._require_sdk("batch_place_orders")
        if not orders:
            return []

        results: list[Any] = []
        successes = 0
        failures = 0
        batch_start_ns = time.perf_counter_ns()

        for i, order in enumerate(orders):
            if i > 0:
                time.sleep(0.067)  # Rate limit: max 15 orders/sec
            try:
                result = self.place_order(
                    symbol=order.get("symbol"),
                    price=order.get("price"),
                    qty=order.get("qty"),
                    side=order.get("side"),
                    tif=order.get("tif", "ROD"),
                    price_type=order.get("price_type", "LMT"),
                    order_type=order.get("order_type", "Stock"),
                )
                results.append(result)
                successes += 1
            except Exception as exc:
                self.log.error(
                    "fubon_batch_order_failed",
                    index=i,
                    symbol=order.get("symbol"),
                    error=str(exc),
                )
                results.append(None)
                failures += 1

        elapsed_us = (time.perf_counter_ns() - batch_start_ns) / 1000
        self.log.info(
            "fubon_batch_place_orders",
            count=len(orders),
            successes=successes,
            failures=failures,
            elapsed_us=elapsed_us,
        )
        return results
