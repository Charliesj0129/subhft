from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    pass


class PriceScaleProvider(Protocol):
    def price_scale(self, symbol: str) -> int: ...


@dataclass(slots=True)
class SymbolMetadataPriceScaleProvider:
    metadata: Any | None = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            from hft_platform.feed_adapter.normalizer import SymbolMetadata

            self.metadata = SymbolMetadata()

    def price_scale(self, symbol: str) -> int:
        return int(self.metadata.price_scale(symbol)) if self.metadata else 1


@dataclass(slots=True)
class FixedPriceScaleProvider:
    scale: int = 10_000

    def price_scale(self, symbol: str) -> int:
        return int(self.scale) if self.scale else 1


@dataclass(slots=True)
class PriceCodec:
    provider: PriceScaleProvider

    def _scale(self, symbol: str) -> int:
        try:
            scale = int(self.provider.price_scale(symbol))
        except Exception:
            scale = 0
        return scale or 1

    def scale_factor(self, symbol: str) -> int:
        return self._scale(symbol)

    def scale(self, symbol: str, price: Decimal | float | int) -> int:
        """Scale price to integer. Prefer Decimal input for precision."""
        if isinstance(price, Decimal):
            return int(price * Decimal(self._scale(symbol)))
        # Legacy float/int: convert via str to avoid float precision loss
        return int(Decimal(str(price)) * Decimal(self._scale(symbol)))

    def scale_decimal(self, symbol: str, price: Decimal) -> int:
        """Scale a Decimal price to integer, preserving precision until final conversion."""
        return int(price * Decimal(self._scale(symbol)))

    def descale(self, symbol: str, value: int) -> float:
        """Descale integer to float for broker API compatibility.

        Note: Returns float for backward compat with broker APIs that expect float.
        For precision-critical operations, use descale_decimal() instead.
        """
        # Use Decimal for division, then convert to float at the end
        result = Decimal(value) / Decimal(self._scale(symbol))
        return float(result)

    def descale_decimal(self, symbol: str, value: int) -> Decimal:
        """Descale an integer to Decimal for precision-preserving operations."""
        return Decimal(value) / Decimal(self._scale(symbol))
