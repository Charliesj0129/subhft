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
- A cancel of an order that is already filled is refused with the message
  ``期貨委託目前狀態成交單已不允許取消交易`` (seen on the official test environment); it
  is raised as :class:`FubonOrderAlreadyFilledError` so the gateway can tell it from a
  failure to cancel a live order.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable
from typing import Any

from hft_platform.core import timebase

_MESSAGE_LIMIT = 200

# Part of the broker's refusal of a cancel on a filled order (``成交單已不允許取消交易``:
# "a filled order no longer allows cancel").  The Result envelope has no error code, so the
# text is the only signal; it names the order state (filled), so a partly filled order that
# can still be cancelled does not produce it.
_ALREADY_FILLED_CANCEL_MARKERS: tuple[str, ...] = ("成交單已不允許取消",)

# Platform prices are scaled int x10000 (Law 4); the SDK wants a decimal string.
PRICE_SCALE = 10_000
_PRICE_DECIMALS = 4

# ``user_def`` is echoed back on order and fill reports.  The SDK rejects other
# characters (and, per its own error text, anything but short ASCII tokens).
USER_DEF_MAX_LEN = 10


class FubonSdkResultError(RuntimeError):
    """The Fubon SDK answered with a failed or malformed ``Result`` envelope."""


class FubonOrderAlreadyFilledError(FubonSdkResultError):
    """A cancel was refused because the order is already filled (nothing left to cancel)."""


def unwrap_sdk_result(result: Any, op: str) -> Any:
    """Return ``result.data`` or raise when the SDK did not report success.

    A failed envelope must never be read as an empty answer: for position
    queries that would turn "broker said no" into "confirmed flat".
    """
    if result is None or not hasattr(result, "is_success"):
        raise FubonSdkResultError(f"{op}: unexpected SDK result shape ({type(result).__name__})")
    if result.is_success is not True:
        full_message = str(getattr(result, "message", None))
        message = full_message[:_MESSAGE_LIMIT]
        if op == "cancel_order" and any(marker in full_message for marker in _ALREADY_FILLED_CANCEL_MARKERS):
            raise FubonOrderAlreadyFilledError(f"{op}: SDK reported failure: {message}")
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
    if isinstance(name, str):
        return name
    # fubon_neo's pyo3 enums have no .name; str() gives "PriceType.Limit".
    return str(value).rpartition(".")[2]


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


# OrderAdapter kwargs that carry nothing the futopt order needs: the exchange is
# implied by sdk.futopt, and intent is the platform's own record (its price is in
# the symbol's scale, not x10000). order_type is the TIF repeated (OrderAdapter
# passes tif_str for both), not an open/close flag. product_type is NOT ignored: it
# is what picks Future versus Option in market_type.
_ADAPTER_IGNORED_KWARGS = frozenset({"exchange", "intent", "order_type"})
_ADAPTER_PRICE_TYPES = {"LMT": "LMT", "MKT": "MKT", "MKP": "MKP", "Limit": "LMT", "Market": "MKT", "RangeMarket": "MKP"}
_ADAPTER_OC_TYPES = {"Auto": "Auto", "New": "New", "Cover": "Close", "Close": "Close", "DayTrade": "FdayTrade"}

# SymbolMetadata.product_type() yields lower-case "future"/"option" (config spelling
# "futures"/"options"/"opt" also occurs); anything else (stock, index, "") is not a futopt.
_FUTURE_PRODUCT_TYPES = frozenset({"future", "futures", "fut"})
_OPTION_PRODUCT_TYPES = frozenset({"option", "options", "opt"})

# TAIFEX night session is 15:00 to 05:00 Asia/Taipei, half-open [open, close): 05:00 is
# already outside it (same boundaries as MarketCalendar). Taipei is a fixed UTC+8 (no
# DST), so the session is told from the epoch with integer arithmetic, no datetime.
_NS_PER_SECOND = 1_000_000_000
_SECONDS_PER_DAY = 86_400
_TAIPEI_UTC_OFFSET_S = 8 * 3600
_NIGHT_OPEN_S = 15 * 3600
_NIGHT_CLOSE_S = 5 * 3600


def is_taifex_night_session(epoch_ns: int) -> bool:
    """Return True when *epoch_ns* (UTC epoch nanoseconds) is inside the TAIFEX night session.

    The window is [15:00, 05:00) Asia/Taipei, so 15:00:00 is night and 05:00:00 is not.
    The gap between close and the day open (05:00 to 08:45) and the gap after the day
    close are not night: they stay on the day market, where the broker refuses the
    order while it is closed.
    """
    local_s = (epoch_ns // _NS_PER_SECOND + _TAIPEI_UTC_OFFSET_S) % _SECONDS_PER_DAY
    return local_s >= _NIGHT_OPEN_S or local_s < _NIGHT_CLOSE_S


def resolve_futopt_market_type(product_type: Any, epoch_ns: int) -> str:
    """Pick the futopt market (``Future``/``FutureNight``/``Option``/``OptionNight``).

    Raises:
        ValueError: *product_type* is neither a future nor an option (fail closed: an
            order is never sent to a market that was guessed).
    """
    name = "" if product_type is None else _enum_name(product_type).strip().lower()
    if name in _FUTURE_PRODUCT_TYPES:
        base = "Future"
    elif name in _OPTION_PRODUCT_TYPES:
        base = "Option"
    else:
        raise ValueError(f"Cannot pick a futopt market: product_type must be future or option, got {product_type!r}")
    return f"{base}Night" if is_taifex_night_session(epoch_ns) else base


def float_price_to_scaled(price: Any) -> int:
    """Turn OrderAdapter's descaled float price into a x10000 int via its 4-decimal text."""
    if isinstance(price, bool) or not isinstance(price, int | float) or not math.isfinite(price):
        raise ValueError(f"price must be a finite number, got {price!r}")
    whole, _, frac = f"{price:.{_PRICE_DECIMALS}f}".partition(".")
    sign = -1 if whole.startswith("-") else 1
    return sign * (abs(int(whole)) * PRICE_SCALE + int(frac or "0"))


def translate_adapter_order_kwargs(
    kwargs: dict[str, Any],
    *,
    now_ns: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """Map OrderAdapter.place_order kwargs onto FubonOrderGateway.place_order kwargs.

    contract_code -> symbol, action -> side, custom_field -> user_def (the token the
    router backfills into order_id_map), price float -> scaled int, oc_type -> order_type,
    and product_type plus the clock -> market_type (Future/FutureNight/Option/OptionNight).

    Args:
        kwargs: What OrderAdapter passes to place_order.
        now_ns: Wall clock in UTC epoch nanoseconds, used to tell the day session from the
            night session. Defaults to ``timebase.now_ns``; tests pin it.

    Raises:
        TypeError: a kwarg is neither mapped nor known to be irrelevant (fail closed).
        ValueError: product_type is missing or is not a future or an option, so the market
            cannot be told (fail closed: nothing is guessed), or another value is invalid.
    """
    src = dict(kwargs)
    unknown = sorted(
        set(src)
        - _ADAPTER_IGNORED_KWARGS
        - {"contract_code", "action", "price", "qty", "tif", "custom_field", "price_type", "oc_type", "product_type"}
    )
    if unknown:
        raise TypeError(f"Fubon place_order: unsupported OrderAdapter kwargs {unknown}")
    price_type_name = _enum_name(src.get("price_type", "LMT"))
    if price_type_name not in _ADAPTER_PRICE_TYPES:
        raise ValueError(f"Unknown price type: {price_type_name}")
    oc_type = _enum_name(src.get("oc_type", "Auto"))
    if oc_type not in _ADAPTER_OC_TYPES:
        raise ValueError(f"Unknown oc_type: {oc_type}")
    clock = now_ns if now_ns is not None else timebase.now_ns
    market_type = resolve_futopt_market_type(src.get("product_type"), clock())
    out: dict[str, Any] = {
        "symbol": src.get("contract_code"),
        "price": float_price_to_scaled(src["price"]) if src.get("price") is not None else None,
        "qty": src.get("qty"),
        "side": src.get("action"),
        "tif": src.get("tif", "ROD"),
        "price_type": _ADAPTER_PRICE_TYPES[price_type_name],
        "order_type": _ADAPTER_OC_TYPES[oc_type],
        "user_def": src.get("custom_field"),
        "market_type": market_type,
    }
    return out
