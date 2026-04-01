"""Mark-to-Market Unrealized PnL Calculator (WU-03).

Computes per-position and portfolio-level unrealized PnL using
PositionStore positions and live mid-price quotes.  All arithmetic
uses scaled integers (x10000) — no float for financial values.
"""

from __future__ import annotations

import threading
from typing import Callable

from prometheus_client import Gauge
from structlog import get_logger

from hft_platform.execution.positions import PositionStore

logger = get_logger("mtm")

# Portfolio-level unrealized PnL gauge (scaled int).
portfolio_unrealized_pnl = Gauge(
    "portfolio_unrealized_pnl",
    "Portfolio-level mark-to-market unrealized PnL (scaled int x10000)",
)


class MarkToMarketCalculator:
    """Per-position and portfolio unrealized PnL calculator.

    Parameters
    ----------
    position_store:
        Live PositionStore instance whose ``positions`` dict is read.
    mid_price_fn:
        Callback ``(symbol) -> int | None`` returning the current mid-price
        as a scaled integer, or *None* when no quote is available.
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
        """
        result: dict[str, int] = {}
        with self._lock:
            for key, pos in self._position_store.positions.items():
                if pos.net_qty == 0:
                    result[key] = 0
                    continue

                mid = self._mid_price_fn(pos.symbol)
                if mid is None:
                    logger.warning(
                        "mid_price_unavailable",
                        symbol=pos.symbol,
                        key=key,
                    )
                    continue

                multiplier = self._multiplier_fn(pos.symbol)
                result[key] = self._unrealized(pos.net_qty, pos.avg_price_scaled, mid, multiplier)

        return result

    def total_unrealized_pnl(self) -> int:
        """Portfolio-level sum of unrealized PnL (scaled int).

        Updates the ``portfolio_unrealized_pnl`` Prometheus gauge as a
        side-effect.
        """
        pnl_map = self.calculate()
        total = sum(pnl_map.values())
        portfolio_unrealized_pnl.set(total)
        return total

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
