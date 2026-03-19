"""EOD reconciliation runner (WU-04)."""

import asyncio
import datetime
import os
from typing import Any

from structlog import get_logger

logger = get_logger("execution.eod_recon")


class EODReconciliationRunner:
    __slots__ = (
        "_eod_hour_utc",
        "_poll_interval_s",
        "_last_triggered_day",
        "_order_adapter",
        "_position_store",
        "_running",
    )

    def __init__(self, order_adapter: Any, position_store: Any) -> None:
        self._eod_hour_utc = int(os.getenv("HFT_EOD_CLOSE_HOUR_UTC", "5"))
        self._poll_interval_s = float(os.getenv("HFT_EOD_POLL_INTERVAL_S", "30"))  # precision-ok
        self._last_triggered_day: int = -1
        self._order_adapter = order_adapter
        self._position_store = position_store
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval_s)
                await self._check_trigger()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("EOD recon poll error", error=str(exc))

    def stop(self) -> None:
        self._running = False

    async def _check_trigger(self) -> None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if now_utc.hour != self._eod_hour_utc:
            return
        day_ordinal = now_utc.toordinal()
        if day_ordinal == self._last_triggered_day:
            return
        self._last_triggered_day = day_ordinal
        logger.info("EOD reconciliation triggered", day=day_ordinal)
        drain_fn = getattr(self._order_adapter, "drain_and_cancel", None)
        if drain_fn:
            try:
                await drain_fn(timeout_s=10.0)
            except Exception as exc:
                logger.error("EOD order cancel failed", error=str(exc))
        logger.info("EOD PnL summary", total_pnl=getattr(self._position_store, "total_pnl", 0))
