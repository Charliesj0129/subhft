"""Fill models for maker backtest engine.

Slice B Task 8 wires :class:`QueueDepletionFill` to a calibrated
``QHatTable`` so the previously-literal ``queue_fraction = 0.5`` is replaced by
``q_hat(symbol, hour, depth_bucket)`` whenever a table is supplied. The legacy
positional constructor ``QueueDepletionFill(queue_fraction=0.5)`` keeps working
unchanged (backward compat for ``_gate_c.py:232`` and existing fixtures).

Fallback policy (when a table IS supplied)
------------------------------------------
On a table-cell miss, ``QueueDepletionFill`` uses ``QHatTable.fallback`` (the
table's own documented fallback, default 0.5), NOT the constructor's
``queue_fraction`` argument. Rationale: callers wiring a table opt into the
table's graceful-degradation policy as the single source of truth for fallback.
Mixing the constructor's qf into the missing-cell path would re-introduce two
competing fallbacks and confuse later promotion gates. The constructor's
``queue_fraction`` only matters when no table is supplied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from research.backtest.q_hat_table import QHatTable


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
    """CK-direct fill model: queue depletion tracking.

    Backward-compatible constructor:
        ``QueueDepletionFill(queue_fraction=0.5)`` — pre-Slice-B behavior.

    Slice B Task 8 keyword-only extension:
        ``QueueDepletionFill(queue_fraction=0.5, *, q_hat_table=table,
        symbol="TMFD6", clock=clock_fn)`` — uses calibrated q_hat per
        (symbol, hour-of-day, depth_bucket). When a cell is missing, falls
        through to ``q_hat_table.fallback`` (NOT ``queue_fraction``).

    The new params are keyword-only (after the ``*`` barrier) so positional
    calls at ``_gate_c.py:232`` and elsewhere remain unaffected.
    """

    __slots__ = ("_qf", "_q_hat_table", "_symbol", "_clock")

    def __init__(
        self,
        queue_fraction: float = 0.5,
        *,
        q_hat_table: "QHatTable | None" = None,
        symbol: str = "",
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._qf = queue_fraction
        self._q_hat_table = q_hat_table
        self._symbol = symbol
        # Default clock returns 0 ns -> hour=0; only relevant when a table is
        # supplied without a clock, in which case the caller has effectively
        # asked for the hour=0 cells. This mirrors the calibration harness's
        # epoch-modulo convention (see calibrate_queue_fill._hour_of_day).
        self._clock = clock if clock is not None else (lambda: 0)

    @property
    def label(self) -> str:
        return f"QueueDepletion(qf={self._qf})"

    @property
    def queue_fraction(self) -> float:
        return self._qf

    def post_quote(self, side: str, price: int, book_qty: int) -> QueuePosition:
        # Default to the legacy literal qf so the no-table path is byte-for-byte
        # identical to pre-Slice-B behavior.
        qf = self._qf
        if self._q_hat_table is not None:
            # Hour-of-day must match the calibration harness convention
            # (see research/backtest/calibrate_queue_fill._hour_of_day):
            # epoch-ns -> seconds -> hours mod 24.
            hour = (self._clock() // 1_000_000_000) // 3600 % 24
            # On a missing cell the lookup returns ``q_hat_table.fallback`` —
            # see module docstring for the design rationale (single source
            # of truth for fallback when a table is wired).
            qf = self._q_hat_table.lookup(self._symbol, int(hour), book_qty)
        queue_ahead = max(1, int(book_qty * qf))
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
