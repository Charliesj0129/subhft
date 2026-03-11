"""Shioaji-specific order enum encoding.

Maps canonical platform enums (Side, TIF) to Shioaji SDK string constants.
"""

from __future__ import annotations

from hft_platform.contracts.strategy import TIF, Side

_SIDE_MAP: dict[Side, str] = {
    Side.BUY: "Buy",
    Side.SELL: "Sell",
}

_TIF_MAP: dict[TIF, str] = {
    TIF.LIMIT: "ROD",
    TIF.IOC: "IOC",
    TIF.FOK: "FOK",
    TIF.ROD: "ROD",
}

_PRICE_TYPE_CANONICAL: frozenset[str] = frozenset({"LMT", "MKT", "MKP"})


class ShioajiOrderCodec:
    """Shioaji-specific order enum encoding.

    Stateless codec that translates canonical platform enums into
    string values expected by the Shioaji broker SDK.
    """

    __slots__ = ()

    def encode_side(self, side: Side) -> str:
        """Map canonical Side enum to Shioaji Action string.

        Returns ``"Buy"`` or ``"Sell"``.

        Raises:
            ValueError: If *side* is not a recognised ``Side`` member.
        """
        try:
            return _SIDE_MAP[side]
        except KeyError:
            raise ValueError(f"Unknown side: {side!r}") from None

    def encode_tif(self, tif: TIF) -> str:
        """Map canonical TIF enum to Shioaji order-type string.

        Returns one of ``"ROD"``, ``"IOC"``, ``"FOK"``.

        Raises:
            ValueError: If *tif* is not a recognised ``TIF`` member.
        """
        try:
            return _TIF_MAP[tif]
        except KeyError:
            raise ValueError(f"Unknown TIF: {tif!r}") from None

    def encode_price_type(self, price_type: str) -> str:
        """Validate and normalise a price-type string for Shioaji.

        Accepted inputs (case-insensitive): ``LMT``, ``MKT``, ``MKP``.

        Raises:
            ValueError: If *price_type* is not one of the accepted values.
        """
        normalised = price_type.strip().upper()
        if normalised not in _PRICE_TYPE_CANONICAL:
            raise ValueError(f"Unknown price_type: {price_type!r} (expected one of {sorted(_PRICE_TYPE_CANONICAL)})")
        return normalised
