"""Fubon order execution gateway.

Wraps the Fubon SDK (fubon_neo 2.5.0) futures/options order API: placement,
cancellation, price and lot modification.  All prices are received as scaled int
(x10000) and turned into the decimal *string* the SDK expects by integer
arithmetic at this boundary (no float, no Decimal).

Real shapes (introspected from the 2.5.0 wheel)::

    sdk.futopt.place_order(account, FutOptOrder, unblock=None)   -> Result
    sdk.futopt.cancel_order(account, order_res, unblock=None)    -> Result
    sdk.futopt.make_modify_price_obj(order_res, price=None, price_type=None)
    sdk.futopt.modify_price(account, modify_price_obj, unblock=None)
    sdk.futopt.make_modify_lot_obj(order_res, lot)
    sdk.futopt.modify_lot(account, modify_lot_obj, unblock=None)
    sdk.futopt.get_order_results(account, market_type=None)

``Result`` is ``(is_success, message, data)`` and ``data`` is a
``FutOptOrderResult``; a failed envelope raises here and is never returned as if
it were an order.  Cancel and modify act on a ``FutOptOrderResult`` (what
``place_order`` returns); a bare order number is resolved through
``get_order_results`` first.
"""

from __future__ import annotations

import time
from typing import Any

from structlog import get_logger

from hft_platform.feed_adapter.fubon.order_codec import (
    FubonOrderCodec,
    encode_user_def,
    format_scaled_price,
    unwrap_sdk_result,
)

logger = get_logger("feed_adapter.fubon.order_gateway")

PRICE_SCALE = 10000

# Price types that carry a limit price; the others must be sent without one.
_PRICED_TYPES = frozenset({"LMT"})


class FubonOrderGateway:
    """Fubon futures/options order execution gateway."""

    __slots__ = ("_sdk", "_client", "_codec", "_account", "log")

    def __init__(
        self,
        sdk: Any | None = None,
        codec: FubonOrderCodec | None = None,
        *,
        client: Any | None = None,
        account: Any | None = None,
    ) -> None:
        if sdk is None:
            sdk = client
        self._sdk = sdk
        self._client = sdk
        self._codec = codec or FubonOrderCodec()
        self._account = account
        self.log = logger

    def set_account(self, account: Any) -> None:
        """Bind the trading account returned by login (every futopt call needs it)."""
        self._account = account

    def _require_sdk(self, op: str) -> Any:
        if self._sdk is None:
            raise NotImplementedError(f"FubonOrderGateway.{op} not yet implemented")
        return self._sdk

    def _require_account(self, op: str) -> Any:
        if self._account is None:
            raise RuntimeError(f"FubonOrderGateway.{op}: no trading account (login has not completed)")
        return self._account

    def place_order(
        self,
        symbol: str | None = None,
        price: int | None = None,
        qty: int | None = None,
        side: Any = None,
        tif: Any = "ROD",
        price_type: str = "LMT",
        order_type: str = "Auto",
        *,
        market_type: str = "Future",
        user_def: str | None = None,
    ) -> Any:
        """Place a futures/options order via ``sdk.futopt.place_order``.

        Args:
            symbol: Contract symbol as the exchange lists it (e.g. ``"TMFK6"``).
            price: Limit price as scaled int (x10000); ignored for non-limit types.
            qty: Order quantity in lots.
            side: ``"Buy"``/``"Sell"`` or the platform ``Side`` enum.
            tif: ``"ROD"``, ``"IOC"``, ``"FOK"`` or the platform ``TIF`` enum.
            price_type: ``"LMT"``, ``"MKT"`` or ``"MKP"``.
            order_type: ``"Auto"``, ``"New"``, ``"Close"`` or ``"FdayTrade"``.
            market_type: ``"Future"``, ``"FutureNight"``, ``"Option"`` or ``"OptionNight"``.
                The caller picks the session; nothing here guesses it.
            user_def: Optional tag echoed on reports (1-10 ASCII letters/digits).

        Returns:
            The SDK ``FutOptOrderResult``.

        Raises:
            ValueError: an argument is invalid (nothing is sent to the SDK).
            FubonSdkResultError: the SDK reported failure.
        """
        sdk = self._require_sdk("place_order")
        account = self._require_account("place_order")
        if symbol is None or qty is None or side is None:
            raise TypeError("symbol, qty, and side are required")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"qty must be a positive int, got {qty!r}")
        priced = price_type in _PRICED_TYPES
        if priced and (price is None or isinstance(price, bool) or not isinstance(price, int) or price <= 0):
            raise ValueError(f"{price_type} order needs a positive scaled-int price, got {price!r}")
        sdk_price = format_scaled_price(price) if priced else None
        order = self._codec.build_futopt_order(
            market_type=self._codec.encode_futopt_market_type(market_type),
            price_type=self._codec.encode_futopt_price_type(price_type),
            time_in_force=self._codec.encode_tif(tif),
            order_type=self._codec.encode_futopt_order_type(order_type),
            buy_sell=self._codec.encode_side(side),
            symbol=symbol,
            lot=qty,
            price=sdk_price,
            user_def=encode_user_def(user_def),
        )

        start_ns = time.perf_counter_ns()
        try:
            data = unwrap_sdk_result(sdk.futopt.place_order(account, order), "place_order")
            self.log.info(
                "fubon_place_order",
                symbol=symbol,
                side=str(side),
                price_scaled=price,
                qty=qty,
                elapsed_us=(time.perf_counter_ns() - start_ns) / 1000,
            )
            return data
        except Exception as exc:
            self.log.error(
                "fubon_place_order_failed",
                symbol=symbol,
                side=str(side),
                error=str(exc),
                elapsed_us=(time.perf_counter_ns() - start_ns) / 1000,
            )
            raise

    def place_futopt_order(self, *args: Any, **kwargs: Any) -> Any:
        """Alias of :meth:`place_order` (futures/options is the only order path)."""
        return self.place_order(*args, **kwargs)

    def _resolve_order_result(self, trade: Any, op: str) -> Any:
        """Return the ``FutOptOrderResult`` the SDK needs for cancel/modify.

        Accepts the object ``place_order`` returned (has ``order_no``/``seq_no``) as is.
        A bare order-number string, or an object that only has ``order_id``, is
        looked up in today's order results.

        Raises:
            TypeError: *trade* carries no order identity.
            ValueError: the order number is not among today's order results.
        """
        if not isinstance(trade, str) and (
            getattr(trade, "order_no", None) is not None or getattr(trade, "seq_no", None) is not None
        ):
            return trade
        order_id = trade if isinstance(trade, str) else getattr(trade, "order_id", None)
        if order_id is None or not str(order_id).strip():
            raise TypeError(f"{op}: cannot extract an order identity from trade (type={type(trade).__name__})")
        order_id = str(order_id)
        results = unwrap_sdk_result(
            self._require_sdk(op).futopt.get_order_results(self._require_account(op)),
            "get_order_results",
        )
        for candidate in results or ():
            if str(getattr(candidate, "order_no", "")) == order_id or str(getattr(candidate, "seq_no", "")) == order_id:
                return candidate
        raise ValueError(f"{op}: order {order_id} is not among today's order results")

    def cancel_order(self, trade: Any) -> Any:
        """Cancel an existing order via ``sdk.futopt.cancel_order``.

        Args:
            trade: What ``place_order`` returned (a ``FutOptOrderResult``), or an
                order number string.

        Returns:
            The SDK ``FutOptOrderResult`` of the cancellation.
        """
        sdk = self._require_sdk("cancel_order")
        account = self._require_account("cancel_order")
        order_res = self._resolve_order_result(trade, "cancel_order")
        order_id = str(getattr(order_res, "order_no", ""))
        start_ns = time.perf_counter_ns()
        try:
            data = unwrap_sdk_result(sdk.futopt.cancel_order(account, order_res), "cancel_order")
            self.log.info(
                "fubon_cancel_order",
                order_id=order_id,
                elapsed_us=(time.perf_counter_ns() - start_ns) / 1000,
            )
            return data
        except Exception as exc:
            self.log.error(
                "fubon_cancel_order_failed",
                order_id=order_id,
                error=str(exc),
                elapsed_us=(time.perf_counter_ns() - start_ns) / 1000,
            )
            raise

    def update_order(
        self,
        trade: Any,
        price: int | None = None,
        qty: int | None = None,
    ) -> Any:
        """Modify an existing order's price and/or lot.

        Price goes through ``make_modify_price_obj`` + ``modify_price``; lot through
        ``make_modify_lot_obj`` + ``modify_lot``.  With both, the price change runs
        first and the lot change acts on its result; if the second step fails the
        first stays applied (the exception says which step failed).

        Args:
            trade: What ``place_order`` returned, or an order number string.
            price: New limit price as scaled int (x10000).
            qty: New lot, passed to the SDK as its ``lot`` argument unchanged.

        Returns:
            The SDK ``FutOptOrderResult`` of the last step, or None when neither
            ``price`` nor ``qty`` was given.
        """
        if price is None and qty is None:
            self.log.warning("update_order: no price or qty provided, no-op")
            return None
        sdk = self._require_sdk("update_order")
        account = self._require_account("update_order")
        if price is not None and (isinstance(price, bool) or not isinstance(price, int) or price <= 0):
            raise ValueError(f"price must be a positive scaled int, got {price!r}")
        if qty is not None and (isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0):
            raise ValueError(f"qty must be a positive int, got {qty!r}")
        order_res = self._resolve_order_result(trade, "update_order")
        order_id = str(getattr(order_res, "order_no", ""))
        step = "modify_price"
        start_ns = time.perf_counter_ns()
        try:
            if price is not None:
                price_obj = sdk.futopt.make_modify_price_obj(order_res, format_scaled_price(price))
                order_res = unwrap_sdk_result(sdk.futopt.modify_price(account, price_obj), step)
            if qty is not None:
                step = "modify_lot"
                lot_obj = sdk.futopt.make_modify_lot_obj(order_res, qty)
                order_res = unwrap_sdk_result(sdk.futopt.modify_lot(account, lot_obj), step)
            self.log.info(
                "fubon_update_order",
                order_id=order_id,
                price_scaled=price,
                qty=qty,
                elapsed_us=(time.perf_counter_ns() - start_ns) / 1000,
            )
            return order_res
        except Exception as exc:
            self.log.error(
                "fubon_update_order_failed",
                order_id=order_id,
                step=step,
                error=str(exc),
                elapsed_us=(time.perf_counter_ns() - start_ns) / 1000,
            )
            raise

    def batch_place_orders(self, orders: list[dict[str, Any]]) -> list[Any]:
        """Place multiple orders sequentially with per-order error isolation.

        Fubon SDK has no native batch API here, so orders are placed one at a time
        with rate-limit-aware delays (67ms = 1/15s between orders).

        Args:
            orders: List of order dicts, each with keys:
                symbol (str), price (int, scaled x10000), qty (int),
                side (str), tif (str, default "ROD"),
                price_type (str, default "LMT"),
                order_type (str, default "Auto"),
                market_type (str, default "Future"), user_def (optional).

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
                    order_type=order.get("order_type", "Auto"),
                    market_type=order.get("market_type", "Future"),
                    user_def=order.get("user_def"),
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
