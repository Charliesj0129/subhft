"""EOD Settlement Reconciliation Trigger (WU-01).

Async coroutine that triggers ``ReconciliationService.sync_portfolio()`` once
per day at a configurable trading-close hour (UTC).  Default is 05:00 UTC
which corresponds to TWSE 13:00 UTC+8.

Environment variables
---------------------
``HFT_EOD_CLOSE_HOUR_UTC`` : int, default ``5``
    UTC hour at which end-of-day reconciliation fires.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("eod_recon")

# Prometheus gauge values
_STATUS_PENDING = 0
_STATUS_SUCCESS = 1
_STATUS_FAILURE = 2

# Poll interval inside the run loop (seconds)
_POLL_INTERVAL_S = 30


class EODReconciliationRunner:
    """Triggers end-of-day settlement reconciliation at a configurable UTC hour.

    Attributes
    ----------
    close_hour_utc : int
        UTC hour (0-23) when EOD reconciliation fires.
    running : bool
        Whether the run-loop is active.
    """

    __slots__ = (
        "close_hour_utc",
        "running",
        "_recon_service",
        "_last_trigger_date",
        "_eod_recon_status",
        "_eod_recon_last_ts",
    )

    def __init__(
        self,
        recon_service: ReconciliationService,
        close_hour_utc: int | None = None,
    ) -> None:
        self._recon_service = recon_service
        self.close_hour_utc = (
            close_hour_utc if close_hour_utc is not None else int(os.environ.get("HFT_EOD_CLOSE_HOUR_UTC", "5"))
        )
        self.running: bool = False
        self._last_trigger_date: str = ""

        # Register Prometheus gauges
        metrics = MetricsRegistry.get()
        self._eod_recon_status = _get_or_create_gauge(
            metrics,
            "eod_recon_status",
            "EOD reconciliation status (0=pending, 1=success, 2=failure)",
        )
        self._eod_recon_last_ts = _get_or_create_gauge(
            metrics,
            "eod_recon_last_ts",
            "Unix timestamp of last EOD reconciliation attempt",
        )
        self._eod_recon_status.set(_STATUS_PENDING)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop -- poll until stopped."""
        self.running = True
        logger.info(
            "EODReconciliationRunner started",
            close_hour_utc=self.close_hour_utc,
        )

        while self.running:
            await asyncio.sleep(_POLL_INTERVAL_S)
            if not self.running:
                break
            await self._check_and_trigger()

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self.running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _check_and_trigger(self) -> None:
        current_utc_hour, today_str = _current_utc_hour_and_date()

        if current_utc_hour != self.close_hour_utc:
            return

        # Once-per-day guard
        if self._last_trigger_date == today_str:
            return

        self._last_trigger_date = today_str
        logger.info(
            "EOD reconciliation triggered",
            date=today_str,
            hour_utc=current_utc_hour,
        )

        try:
            await self._recon_service.sync_portfolio()
            self._eod_recon_status.set(_STATUS_SUCCESS)
            self._eod_recon_last_ts.set(timebase.now_s())
            logger.info("EOD reconciliation completed successfully", date=today_str)
        except Exception as exc:
            self._eod_recon_status.set(_STATUS_FAILURE)
            self._eod_recon_last_ts.set(timebase.now_s())
            logger.error(
                "EOD reconciliation failed",
                date=today_str,
                error=str(exc),
                exc_info=True,
            )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _current_utc_hour_and_date() -> tuple[int, str]:
    """Return (utc_hour, date_string) using time.gmtime to avoid datetime."""
    t = time.gmtime(timebase.now_s())
    return t.tm_hour, f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _get_or_create_gauge(
    metrics: MetricsRegistry,
    name: str,
    doc: str,
) -> Any:
    """Return an existing gauge attribute on *metrics*, or create a new one."""
    from prometheus_client import Gauge

    existing = getattr(metrics, name, None)
    if existing is not None:
        return existing
    gauge = Gauge(name, doc)
    setattr(metrics, name, gauge)
    return gauge
