"""Mark-to-Market Unrealized PnL Calculator (WU-03).

Computes per-position and portfolio-level unrealized PnL using
PositionStore positions and live mid-price quotes.  All arithmetic
uses scaled integers (x10000) — no float for financial values.
"""

from __future__ import annotations

import threading
from typing import Callable, NamedTuple

from prometheus_client import Gauge
from structlog import get_logger

from hft_platform.execution.positions import PositionStore

logger = get_logger("mtm")

# Portfolio-level unrealized PnL gauge (scaled int).
portfolio_unrealized_pnl = Gauge(
    "portfolio_unrealized_pnl",
    "Portfolio-level mark-to-market unrealized PnL (scaled int x10000)",
)


class MtMSnapshot(NamedTuple):
    """A mark-to-market total that says how much of the book it actually covers.

    ``calculate()`` skips any non-flat position whose mid-price is unavailable,
    so a bare total cannot distinguish "the book is flat" from "nothing could be
    priced" -- both are 0. A risk gate that reads the bare total therefore reads
    missing market data as an absence of loss, which is the fail-open direction.

    ``unpriced`` is the count of non-flat positions that had no mid. Callers
    that use the total to *release* a stop must require ``complete``; callers
    that use it to *apply* one may use the partial total, because latching on
    partial data is the safe direction.
    """

    total_scaled: int
    priced: int
    unpriced: int

    @property
    def complete(self) -> bool:
        """True when every non-flat position had a usable mid-price."""
        return self.unpriced == 0


class MarkToMarketCalculator:
    """Per-position and portfolio unrealized PnL calculator.

    Parameters
    ----------
    position_store:
        Live PositionStore instance whose ``positions`` dict is read.
    mid_price_fn:
        Callback ``(symbol) -> int | None`` returning the current mid-price
        as a scaled integer, or *None* when no quote is available.

    Thread safety (Wave 1 documentation, 2026-04-24)
    ------------------------------------------------
    ``self._lock`` is a ``threading.Lock`` reserved for **future** cross-thread
    use. The current production caller is ``HFTSystem._supervise`` running on
    the asyncio event-loop thread (confirmed by the Infra investigator in the
    Wave 1 concurrency audit), so MtM.calculate() is effectively
    single-threaded today. The lock is retained because:

    1. ``_position_store.positions`` is mutated from the ``asyncio.to_thread``
       worker inside ``PositionStore.on_fill_async`` — that race is addressed
       in Wave 3 (expanding ``_fill_lock`` acquisition to PositionStore readers).
       Until Wave 3 lands, this lock does NOT protect the iteration at
       ``calculate()`` from concurrent fill writes.
    2. Future observability callers (Prometheus push gateway, periodic PnL
       reporter) may be added on separate threads; the lock is ready for them.

    Do not remove this lock during Wave 1 — its presence is load-bearing for
    the Wave 3 fix which will coordinate ``_fill_lock`` acquisition with
    MtM iteration to eliminate torn-read PnL.
    """

    __slots__ = ("_position_store", "_mid_price_fn", "_multiplier_fn", "_lock")

    def __init__(
        self,
        position_store: PositionStore,
        mid_price_fn: Callable[[str], int | None],
        multiplier_fn: Callable[[str], int] | None = None,
    ) -> None:
        self._position_store = position_store
        self._mid_price_fn = mid_price_fn
        self._multiplier_fn: Callable[[str], int] = multiplier_fn if multiplier_fn is not None else lambda _: 1
        # See class docstring. Reserved for Wave 3 cross-thread coordination.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(self) -> dict[str, int]:
        """Return unrealized PnL per symbol (scaled int).

        Keys are position-store keys (``account:strategy:symbol``).
        Positions with ``net_qty == 0`` yield ``0``.
        Positions whose mid-price is unavailable are **skipped** (logged
        at warning level).

        Wave 3 (2026-04-25): iterate a snapshot from
        ``PositionStore.snapshot_positions()`` (acquires
        ``_fill_lock`` and ``dataclasses.replace()``-copies every
        Position) instead of ``self._position_store.positions`` directly.
        This removes the cross-thread torn-read race documented in the
        class docstring.
        """
        return self._evaluate()[0]

    def _evaluate(self) -> tuple[dict[str, int], int]:
        """Single pass: per-position PnL, plus how many non-flat ones had no mid."""
        result: dict[str, int] = {}
        unpriced = 0
        snapshot = self._position_store.snapshot_positions()
        with self._lock:
            for key, pos in snapshot.items():
                if pos.net_qty == 0:
                    result[key] = 0
                    continue

                mid = self._mid_price_fn(pos.symbol)
                if mid is None:
                    unpriced += 1
                    logger.warning(
                        "mid_price_unavailable",
                        symbol=pos.symbol,
                        key=key,
                    )
                    continue

                multiplier = self._multiplier_fn(pos.symbol)
                result[key] = self._unrealized(pos.net_qty, pos.avg_price_scaled, mid, multiplier)

        return result, unpriced

    def snapshot(self) -> MtMSnapshot:
        """Portfolio unrealized PnL together with how complete the valuation is.

        Prefer this over ``total_unrealized_pnl()`` anywhere the number
        authorizes something. See ``MtMSnapshot``.

        One pass over one position snapshot: counting the unpriced positions
        separately would read the store twice and could straddle a fill, so the
        total and the completeness claim would not describe the same book.

        Updates the ``portfolio_unrealized_pnl`` Prometheus gauge as a
        side-effect.
        """
        pnl_map, unpriced = self._evaluate()
        total = sum(pnl_map.values())
        portfolio_unrealized_pnl.set(total)
        return MtMSnapshot(total_scaled=total, priced=len(pnl_map), unpriced=unpriced)

    def total_unrealized_pnl(self) -> int:
        """Portfolio-level sum of unrealized PnL (scaled int).

        Carries no completeness information: an all-unpriced book and a flat
        book both return 0. Use ``snapshot()`` when the caller acts on that
        difference.

        Updates the ``portfolio_unrealized_pnl`` Prometheus gauge as a
        side-effect.
        """
        return self.snapshot().total_scaled

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unrealized(net_qty: int, avg_price_scaled: int, mid: int, contract_multiplier: int = 1) -> int:
        """Compute unrealized PnL for a single position (scaled int).

        Long  (net_qty > 0): ``(mid - avg) * qty * contract_multiplier``
        Short (net_qty < 0): ``(avg - mid) * |qty| * contract_multiplier``

        Args:
            contract_multiplier: Contract point value. Stocks=1, Futures=point_value
                (e.g. TMF=10, MXF=50, TXF=200). Default 1 for backward compatibility.
        """
        if net_qty > 0:
            return (mid - avg_price_scaled) * net_qty * contract_multiplier
        # net_qty < 0  (caller already guards == 0)
        return (avg_price_scaled - mid) * (-net_qty) * contract_multiplier
