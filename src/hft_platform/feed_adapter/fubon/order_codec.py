"""Fubon-specific order enum encoding and SDK result decoding.

Maps canonical HFT platform order enums to Fubon SDK (fubon_neo 2.5.0)
constants and builds the ``FutOptOrder`` the futures/options API expects.
Lazy-imports fubon_neo to avoid an import-time dependency.

Real shapes (fubon-neo 2.5.0, introspected from the wheel):

- ``FutOptOrder(market_type, price_type, time_in_force, order_type, buy_sell,
  symbol, buy_sell2=None, symbol2=None, lot=None, price=None, user_def=None)``
  where ``price`` is a **string** (the vendor examples pass ``"530"``).
- ``FutOptMarketType``: Future / FutureNight / Option / OptionNight.
- ``FutOptPriceType``: Limit / Market / RangeMarket / Reference.
- ``FutOptOrderType``: Auto / New / Close / FdayTrade.
- Every trading call returns a ``Result`` envelope
  (``is_success``, ``message``, ``data``); see :func:`unwrap_sdk_result`.
"""

from __future__ import annotations

import operator
from typing import Any

_MESSAGE_LIMIT = 200

# Platform prices are scaled int x10000 (Law 4); the SDK wants a decimal string.
PRICE_SCALE = 10_000
_PRICE_DECIMALS = 4

# ``user_def`` is echoed back on order and fill reports.  The SDK rejects other
# characters (and, per its own error text, anything but short ASCII tokens).
USER_DEF_MAX_LEN = 10


class FubonSdkResultError(RuntimeError):
    """The Fubon SDK answered with a failed or malformed ``Result`` envelope."""


def unwrap_sdk_result(result: Any, op: str) -> Any:
    """Return ``result.data`` or raise when the SDK did not report success.

    A failed envelope must never be read as an empty answer: for position
    queries that would turn "broker said no" into "confirmed flat".
    """
    if result is None or not hasattr(result, "is_success"):
        raise FubonSdkResultError(f"{op}: unexpected SDK result shape ({type(result).__name__})")
    if result.is_success is not True:
        message = str(getattr(result, "message", None))[:_MESSAGE_LIMIT]
        raise FubonSdkResultError(f"{op}: SDK reported failure: {message}")
    return getattr(result, "data", None)


def format_scaled_price(price: Any) -> str:
    """Render a scaled-int (x10000) price as the decimal string the SDK expects.

    Pure integer arithmetic: no float, no Decimal (Law 4).

    Raises:
        TypeError: *price* is not an integer (bool and float are refused).
    """
    if isinstance(price, bool):
        raise TypeError(f"price must be a scaled int (x{PRICE_SCALE}), got bool")
    try:
        value = operator.index(price)
    except TypeError:
        raise TypeError(f"price must be a scaled int (x{PRICE_SCALE}), got {type(price).__name__}") from None
    sign = "-" if value < 0 else ""
    whole, frac = divmod(abs(value), PRICE_SCALE)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:0{_PRICE_DECIMALS}d}".rstrip("0")


def encode_user_def(user_def: Any) -> str | None:
    """Validate the caller tag carried through the SDK (``None`` stays ``None``).

    Raises:
        ValueError: longer than 10 characters or not ASCII letters/digits.
    """
    if user_def is None:
        return None
    text = str(user_def)
    if not text or len(text) > USER_DEF_MAX_LEN or not (text.isascii() and text.isalnum()):
        raise ValueError(f"user_def must be 1-{USER_DEF_MAX_LEN} ASCII letters/digits")
    return text


def _enum_name(value: Any) -> str:
    """Return the member name of a platform enum, or the string itself."""
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value)


class FubonOrderCodec:
    """Fubon-specific order enum encoding."""

    __slots__ = ()

    def encode_side(self, side: Any) -> Any:
        """Map canonical side to Fubon BSAction enum.

        Args:
            side: ``"Buy"`` / ``"Sell"`` or the platform ``Side`` enum.

        Returns:
            BSAction enum value.

        Raises:
            ValueError: If side is not recognized.
        """
        from fubon_neo.constant import BSAction

        mapping = {"Buy": BSAction.Buy, "BUY": BSAction.Buy, "Sell": BSAction.Sell, "SELL": BSAction.Sell}
        name = _enum_name(side)
        if name not in mapping:
            raise ValueError(f"Unknown side: {side}")
        return mapping[name]

    def encode_tif(self, tif: Any) -> Any:
        """Map canonical TIF to Fubon TimeInForce enum.

        Args:
            tif: ``"ROD"`` / ``"IOC"`` / ``"FOK"`` or the platform ``TIF`` enum
                (``TIF.LIMIT`` is ROD, as in the Shioaji codec).

        Returns:
            TimeInForce enum value.

        Raises:
            ValueError: If tif is not recognized.
        """
        from fubon_neo.constant import TimeInForce

        mapping = {
            "ROD": TimeInForce.ROD,
            "LIMIT": TimeInForce.ROD,
            "IOC": TimeInForce.IOC,
            "FOK": TimeInForce.FOK,
        }
        name = _enum_name(tif)
        if name not in mapping:
            raise ValueError(f"Unknown TIF: {tif}")
        return mapping[name]

    def encode_price_type(self, price_type: str) -> Any:
        """Map canonical price type to the *stock* Fubon PriceType enum.

        Futures/options orders must use :meth:`encode_futopt_price_type`.

        Raises:
            ValueError: If price_type is not recognized.
        """
        from fubon_neo.constant import PriceType

        mapping = {"LMT": PriceType.Limit, "MKT": PriceType.Market}
        if price_type not in mapping:
            raise ValueError(f"Unknown price type: {price_type}")
        return mapping[price_type]

    def encode_order_type(self, order_type: str) -> Any:
        """Map canonical order type to the *stock* Fubon OrderType enum.

        Futures/options orders must use :meth:`encode_futopt_order_type`.

        Raises:
            ValueError: If order_type is not recognized.
        """
        from fubon_neo.constant import OrderType

        mapping = {
            "Stock": OrderType.Stock,
            "DayTrade": OrderType.DayTrade,
            "Margin": OrderType.Margin,
        }
        if order_type not in mapping:
            raise ValueError(f"Unknown order type: {order_type}")
        return mapping[order_type]

    def encode_futopt_price_type(self, price_type: str) -> Any:
        """Map canonical price type to ``FutOptPriceType``.

        ``LMT`` is Limit, ``MKT`` is Market, ``MKP`` is RangeMarket (the
        exchange's market-with-protection order, named as in the Shioaji codec).

        Raises:
            ValueError: If price_type is not recognized.
        """
        from fubon_neo.constant import FutOptPriceType

        mapping = {
            "LMT": FutOptPriceType.Limit,
            "MKT": FutOptPriceType.Market,
            "MKP": FutOptPriceType.RangeMarket,
        }
        if price_type not in mapping:
            raise ValueError(f"Unknown price type: {price_type}")
        return mapping[price_type]

    def encode_futopt_order_type(self, order_type: str) -> Any:
        """Map an open/close intent to ``FutOptOrderType``.

        Accepted: ``Auto`` (broker decides open or close), ``New`` (open),
        ``Close``, ``FdayTrade`` (day-trade close).

        Raises:
            ValueError: If order_type is not recognized.
        """
        from fubon_neo.constant import FutOptOrderType

        mapping = {
            "Auto": FutOptOrderType.Auto,
            "New": FutOptOrderType.New,
            "Close": FutOptOrderType.Close,
            "FdayTrade": FutOptOrderType.FdayTrade,
        }
        if order_type not in mapping:
            raise ValueError(f"Unknown futopt order type: {order_type}")
        return mapping[order_type]

    def encode_futopt_market_type(self, market_type: str) -> Any:
        """Map a market name to ``FutOptMarketType``.

        Accepted: ``Future``, ``FutureNight``, ``Option``, ``OptionNight``.
        The caller chooses day or night session; nothing here guesses it.

        Raises:
            ValueError: If market_type is not recognized.
        """
        from fubon_neo.constant import FutOptMarketType

        mapping = {
            "Future": FutOptMarketType.Future,
            "FutureNight": FutOptMarketType.FutureNight,
            "Option": FutOptMarketType.Option,
            "OptionNight": FutOptMarketType.OptionNight,
        }
        if market_type not in mapping:
            raise ValueError(f"Unknown futopt market type: {market_type}")
        return mapping[market_type]

    def build_futopt_order(
        self,
        *,
        market_type: Any,
        price_type: Any,
        time_in_force: Any,
        order_type: Any,
        buy_sell: Any,
        symbol: str,
        lot: int,
        price: str | None = None,
        user_def: str | None = None,
    ) -> Any:
        """Build the SDK ``FutOptOrder`` from already-encoded enums.

        ``price`` is the decimal **string** the SDK expects, or ``None`` for a
        market-type order.
        """
        from fubon_neo.sdk import FutOptOrder

        return FutOptOrder(
            market_type=market_type,
            price_type=price_type,
            time_in_force=time_in_force,
            order_type=order_type,
            buy_sell=buy_sell,
            symbol=symbol,
            lot=lot,
            price=price,
            user_def=user_def,
        )
