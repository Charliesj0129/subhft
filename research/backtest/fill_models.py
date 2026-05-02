"""Fill models for maker backtest engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class QueuePosition:
    """Tracks a single outstanding order's queue position."""

    side: str
    price: int
    queue_ahead: int


@dataclass(frozen=True)
class Fill:
    """A single fill event."""

    side: str
    price: int
    qty: int = 1


class FillModel(Protocol):
    """Protocol for fill models."""

    @property
    def label(self) -> str: ...

    def post_quote(self, side: str, price: int, book_qty: int) -> QueuePosition: ...

    def check_fills(
        self,
        positions: list[QueuePosition],
        trade_price: int,
        trade_volume: int,
    ) -> list[Fill]: ...


class QueueDepletionFill:
    """CK-direct fill model: queue depletion tracking."""

    __slots__ = ("_qf",)

    def __init__(self, queue_fraction: float = 0.5) -> None:
        self._qf = queue_fraction

    @property
    def label(self) -> str:
        return f"QueueDepletion(qf={self._qf})"

    @property
    def queue_fraction(self) -> float:
        return self._qf

    def post_quote(self, side: str, price: int, book_qty: int) -> QueuePosition:
        queue_ahead = max(1, int(book_qty * self._qf))
        return QueuePosition(side=side, price=price, queue_ahead=queue_ahead)

    def check_fills(
        self,
        positions: list[QueuePosition],
        trade_price: int,
        trade_volume: int,
    ) -> list[Fill]:
        fills: list[Fill] = []
        for pos in positions:
            if pos.side == "buy" and trade_price <= pos.price:
                pos.queue_ahead -= trade_volume
            elif pos.side == "sell" and trade_price >= pos.price:
                pos.queue_ahead -= trade_volume

            if pos.queue_ahead <= 0:
                fills.append(Fill(side=pos.side, price=pos.price))
        return fills
