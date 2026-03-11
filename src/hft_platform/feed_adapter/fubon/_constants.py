"""Fubon SDK constant mappings.

Maps platform-generic action/order-type strings to Fubon Neo SDK enums.
The actual enums are imported lazily to avoid hard dependency on fubon_neo.
"""

from __future__ import annotations

import importlib
from typing import Any

# Platform action strings -> Fubon BSAction
ACTION_MAP: dict[str, str] = {
    "Buy": "Buy",
    "Sell": "Sell",
}

# Platform TIF strings -> Fubon TimeInForce
TIF_MAP: dict[str, str] = {
    "ROD": "ROD",
    "IOC": "IOC",
    "FOK": "FOK",
}

# Platform order type strings -> Fubon OrderType
ORDER_TYPE_MAP: dict[str, str] = {
    "stock": "Stock",
    "odd_lot": "OddLot",
    "futures": "Futures",
}

# Platform price type strings -> Fubon PriceType
PRICE_TYPE_MAP: dict[str, str] = {
    "LMT": "Limit",
    "MKT": "Market",
    "limit": "Limit",
    "market": "Market",
    "limit_up": "LimitUp",
    "limit_down": "LimitDown",
}

# Fubon order status codes -> platform status strings
ORDER_STATUS_MAP: dict[int, str] = {
    4: "acknowledged",
    8: "preparing",
    10: "confirmed",
    19: "modify_price_failed",
    29: "modify_qty_failed",
    30: "cancelled",
    39: "cancel_failed",
    40: "partial_cancelled",
    90: "failed",
}


def resolve_fubon_enum(module_name: str, enum_name: str, value: str) -> Any:
    """Lazily resolve a Fubon SDK enum value.

    Example::

        resolve_fubon_enum("fubon_neo.constant", "BSAction", "Buy")

    Raises ``RuntimeError`` when the SDK is not installed or the enum
    member does not exist.
    """
    try:
        mod = importlib.import_module(module_name)
        enum_cls = getattr(mod, enum_name)
        return getattr(enum_cls, value)
    except (ImportError, AttributeError) as e:
        raise RuntimeError(
            f"Cannot resolve Fubon enum {module_name}.{enum_name}.{value}: {e}"
        ) from e
