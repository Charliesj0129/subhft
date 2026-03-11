"""Fubon order gateway -- implements OrderExecutor protocol."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from structlog import get_logger

from hft_platform.feed_adapter.fubon._config import (
    DEFAULT_EXCHANGE,
    DEFAULT_USER_DEF,
    PRICE_SCALE,
)

logger = get_logger("fubon.order")

# Constant mappings (lazy-loaded to avoid hard dependency on fubon_neo)
_ACTION_MAP: dict[str, str] = {"Buy": "Buy", "Sell": "Sell"}
_TIF_MAP: dict[str, str] = {"ROD": "ROD", "IOC": "IOC", "FOK": "FOK"}
_PRICE_TYPE_MAP: dict[str, str] = {
    "LMT": "Limit",
    "limit": "Limit",
    "MKT": "Market",
    "market": "Market",
}


def _get_fubon_constants() -> Any:
    """Lazily import Fubon constants to avoid hard dependency."""
    try:
        from fubon_neo import constant  # type: ignore[import-untyped]

        return constant
    except ImportError as exc:
        raise RuntimeError("fubon-neo not installed") from exc


def _get_order_class() -> Any:
    """Lazily import Fubon Order class."""
    try:
        from fubon_neo.sdk import Order  # type: ignore[import-untyped]

        return Order
    except ImportError as exc:
        raise RuntimeError("fubon-neo not installed") from exc


def _scaled_int_to_price_str(scaled_price: int) -> str:
    """Convert a scaled-int price (x10000) to a decimal string.

    Uses ``Decimal`` arithmetic to avoid float precision loss
    (Precision Law).
    """
    return str(Decimal(scaled_price) / PRICE_SCALE)


class FubonOrderGateway:
    """Fubon order placement, cancellation, and modification.

    Satisfies the OrderExecutor protocol defined in
    ``hft_platform.order.adapter``.
    """

    __slots__ = (
        "_sdk",
        "_account",
        "_symbols_meta",
        "_on_order_cb",
        "_on_deal_cb",
    )

    def __init__(
        self,
        sdk: Any,
        account: Any,
        symbols_meta: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._sdk = sdk
        self._account = account
        self._symbols_meta: dict[str, dict[str, str]] = symbols_meta or {}
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None

    # ------------------------------------------------------------------
    # OrderExecutor interface
    # ------------------------------------------------------------------

    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: float,
        qty: int,
        order_type: str = "stock",
        tif: str = "ROD",
        **kwargs: Any,
    ) -> Any:
        """Place an order via Fubon Neo SDK.

        *price* arrives as a scaled integer (x10000) from the platform.
        Fubon expects a price string, so we convert via ``Decimal`` to
        preserve precision (Precision Law).
        """
        const = _get_fubon_constants()
        Order = _get_order_class()

        # Map platform strings to Fubon enums
        bs_action = getattr(const.BSAction, _ACTION_MAP.get(action, action))
        time_in_force = getattr(const.TimeInForce, _TIF_MAP.get(tif, tif))
        price_type_str = _PRICE_TYPE_MAP.get(order_type, "Limit")
        fubon_price_type = getattr(const.PriceType, price_type_str)

        # Convert scaled-int price to string (Precision Law: no float arithmetic)
        price_str: str | None
        if price and int(price) > 0:
            price_str = _scaled_int_to_price_str(int(price))
        else:
            price_str = None  # Market order

        order = Order(
            buy_sell=bs_action,
            symbol=contract_code,
            price=price_str,
            quantity=qty,
            market_type=const.MarketType.Common,
            price_type=fubon_price_type,
            time_in_force=time_in_force,
            order_type=const.OrderType.Stock,
            user_def=kwargs.get("user_def", DEFAULT_USER_DEF),
        )

        logger.info(
            "fubon_place_order",
            symbol=contract_code,
            action=action,
            price=price_str,
            qty=qty,
            tif=tif,
        )

        return self._sdk.stock.place_order(self._account, order)

    def cancel_order(self, trade: Any) -> Any:
        """Cancel an existing order."""
        logger.info("fubon_cancel_order", order=str(trade))
        return self._sdk.stock.cancel_order(self._account, trade)

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        """Modify an existing order's price or quantity."""
        if price is not None:
            price_str = _scaled_int_to_price_str(int(price))
            modified = self._sdk.stock.make_modify_price_obj(trade, price_str)
            logger.info("fubon_modify_price", new_price=price_str)
            return self._sdk.stock.modify_price(self._account, modified)

        if qty is not None:
            modified = self._sdk.stock.make_modify_volume_obj(trade, qty)
            logger.info("fubon_modify_qty", new_qty=qty)
            return self._sdk.stock.modify_volume(self._account, modified)

        return None

    def get_exchange(self, symbol: str) -> str:
        """Return exchange for *symbol* from metadata, defaulting to TSE."""
        meta = self._symbols_meta.get(symbol, {})
        return meta.get("exchange", DEFAULT_EXCHANGE)

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Register order-status and deal-notification callbacks."""
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal
        logger.info("fubon_execution_callbacks_set")
