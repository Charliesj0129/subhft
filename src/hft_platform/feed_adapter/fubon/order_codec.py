"""Fubon-specific order enum encoding.

Maps canonical HFT platform order enums to Fubon SDK (fubon_neo) constants.
Lazy-imports fubon_neo to avoid import-time dependency.
"""

from __future__ import annotations

from typing import Any


class FubonOrderCodec:
    """Fubon-specific order enum encoding."""

    __slots__ = ()

    def encode_side(self, side: str) -> Any:
        """Map canonical side to Fubon BSAction enum.

        Args:
            side: "Buy" or "Sell".

        Returns:
            BSAction enum value.

        Raises:
            ValueError: If side is not recognized.
        """
        from fubon_neo.constant import BSAction

        mapping = {"Buy": BSAction.Buy, "Sell": BSAction.Sell}
        if side not in mapping:
            raise ValueError(f"Unknown side: {side}")
        return mapping[side]

    def encode_tif(self, tif: str) -> Any:
        """Map canonical TIF to Fubon TimeInForce enum.

        Args:
            tif: "ROD", "IOC", or "FOK".

        Returns:
            TimeInForce enum value.

        Raises:
            ValueError: If tif is not recognized.
        """
        from fubon_neo.constant import TimeInForce

        mapping = {
            "ROD": TimeInForce.ROD,
            "IOC": TimeInForce.IOC,
            "FOK": TimeInForce.FOK,
        }
        if tif not in mapping:
            raise ValueError(f"Unknown TIF: {tif}")
        return mapping[tif]

    def encode_price_type(self, price_type: str) -> Any:
        """Map canonical price type to Fubon PriceType enum.

        Args:
            price_type: "LMT" or "MKT".

        Returns:
            PriceType enum value.

        Raises:
            ValueError: If price_type is not recognized.
        """
        from fubon_neo.constant import PriceType

        mapping = {"LMT": PriceType.Limit, "MKT": PriceType.Market}
        if price_type not in mapping:
            raise ValueError(f"Unknown price type: {price_type}")
        return mapping[price_type]

    def encode_order_type(self, order_type: str) -> Any:
        """Map canonical order type to Fubon OrderType enum.

        Args:
            order_type: "Stock", "DayTrade", or "Margin".

        Returns:
            OrderType enum value.

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
