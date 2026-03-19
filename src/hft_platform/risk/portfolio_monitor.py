"""Portfolio-level risk metrics exporter (WU-10).

Periodic async task that reads PositionStore and publishes
Prometheus gauges for portfolio gross/net exposure, open positions,
concentration ratio, and unrealized PnL.

All financial arithmetic uses scaled integers (Precision Law).
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Callable

from prometheus_client import Gauge
from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.execution.positions import PositionStore

logger = get_logger("portfolio_monitor")

# ---------------------------------------------------------------------------
# Prometheus gauges — registered once at module level
# ---------------------------------------------------------------------------
portfolio_gross_exposure = Gauge(
    "portfolio_gross_exposure",
    "Sum of abs(net_qty * mid_price) across all positions (scaled int)",
)
portfolio_net_exposure = Gauge(
    "portfolio_net_exposure",
    "Sum of signed(net_qty * mid_price) across all positions (scaled int)",
)
portfolio_open_positions = Gauge(
    "portfolio_open_positions",
    "Number of positions with non-zero net_qty",
)
portfolio_concentration_ratio = Gauge(
    "portfolio_concentration_ratio",
    "Largest single-position abs exposure / gross exposure (0-1)",
)
portfolio_unrealized_pnl = Gauge(
    "portfolio_unrealized_pnl",
    "Sum of unrealized PnL across all positions (scaled int)",
)


class PortfolioRiskMonitor:
    """Read-only periodic monitor over PositionStore.

    Parameters
    ----------
    position_store:
        The shared ``PositionStore`` instance (thread-safe reads via its lock).
    mid_price_cb:
        Optional callback ``(symbol: str) -> int | None`` returning the
        latest mid-price as a scaled integer, or ``None`` if unavailable.
    """

    __slots__ = (
        "_position_store",
        "_mid_price_cb",
        "_interval_s",
        "running",
        "_log",
    )

    def __init__(
        self,
        position_store: PositionStore,
        mid_price_cb: Callable[[str], int | None] | None = None,
    ) -> None:
        self._position_store = position_store
        self._mid_price_cb = mid_price_cb
        self._interval_s: float = float(
            os.getenv("HFT_PORTFOLIO_MONITOR_INTERVAL_S", "5")
        )
        self.running: bool = False
        self._log = logger.bind(component="portfolio_monitor")

    # ------------------------------------------------------------------
    # Core snapshot logic (pure, no IO)
    # ------------------------------------------------------------------

    def _snapshot(self) -> None:
        """Compute and publish portfolio risk metrics from current positions."""
        positions = self._position_store.positions

        gross_exposure: int = 0
        net_exposure: int = 0
        open_count: int = 0
        max_single_exposure: int = 0
        total_unrealized_pnl: int = 0

        for pos in positions.values():
            if pos.net_qty == 0:
                continue

            open_count += 1

            mid: int | None = None
            if self._mid_price_cb is not None:
                mid = self._mid_price_cb(pos.symbol)

            if mid is None or mid == 0:
                # Without a valid mid-price we cannot compute exposure
                # for this symbol — skip it but still count as open.
                continue

            # Scaled-int arithmetic: exposure = net_qty * mid_price
            signed_exposure: int = pos.net_qty * mid
            abs_exposure: int = abs(signed_exposure)

            gross_exposure += abs_exposure
            net_exposure += signed_exposure

            if abs_exposure > max_single_exposure:
                max_single_exposure = abs_exposure

            # Unrealized PnL = (mid - avg_price) * net_qty  (scaled int)
            unrealized: int = (mid - pos.avg_price_scaled) * pos.net_qty
            total_unrealized_pnl += unrealized

        # Concentration ratio
        concentration: float = 0.0
        if gross_exposure > 0:
            concentration = max_single_exposure / gross_exposure

        # Publish gauges
        portfolio_gross_exposure.set(gross_exposure)
        portfolio_net_exposure.set(net_exposure)
        portfolio_open_positions.set(open_count)
        portfolio_concentration_ratio.set(concentration)
        portfolio_unrealized_pnl.set(total_unrealized_pnl)

        self._log.debug(
            "portfolio_snapshot",
            gross=gross_exposure,
            net=net_exposure,
            open=open_count,
            concentration=round(concentration, 4),
            unrealized_pnl=total_unrealized_pnl,
        )

    # ------------------------------------------------------------------
    # Async run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the periodic monitor until ``self.running`` is set to False."""
        self.running = True
        self._log.info(
            "portfolio_monitor_started", interval_s=self._interval_s
        )
        try:
            while self.running:
                try:
                    self._snapshot()
                except Exception:
                    self._log.exception("portfolio_snapshot_error")
                await asyncio.sleep(self._interval_s)
        finally:
            self.running = False
            self._log.info("portfolio_monitor_stopped")
