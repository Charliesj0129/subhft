"""Run the read-only trade-cache health probe on a timer.

A missed order or deal report leaves a ``Trade`` on an old status with nothing in the log to say so. The SDK can
report that it saw a gap (``api.trade_cache_health``, shioaji >= 1.7.6); this module asks it once a minute and the
broker client publishes the answer as the ``shioaji_trade_cache_health_*`` metrics (see
``feed_adapter/shioaji/ops_checks.py``).

Telemetry only: nothing here re-logs in, reconciles (``update_status``), or halts. An operator reads the gauges.

The probe is a blocking SDK call, so it runs in a worker thread and never on the engine loop. It is not an SDK
callback and not on the hot path. Interval: ``DEFAULT_INTERVAL_S``, overridable by argument only (no env switch).
"""

from __future__ import annotations

import asyncio
from typing import Any

from structlog import get_logger

logger = get_logger("ops.trade_cache_health_watch")

__all__ = ["DEFAULT_INTERVAL_S", "MIN_INTERVAL_S", "run_trade_cache_health_watch"]

#: Seconds between probes.
DEFAULT_INTERVAL_S = 60.0
#: A smaller requested interval is raised to this, so a misconfigured caller cannot spin the SDK.
MIN_INTERVAL_S = 5.0


async def run_trade_cache_health_watch(order_client: Any, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    """Probe forever; an interval of 0 or less, or a client without the probe, returns at once."""
    probe = getattr(order_client, "check_trade_cache_health", None)
    if not callable(probe):
        logger.info("trade_cache_health_watch_unsupported", client=type(order_client).__name__)
        return
    if interval_s <= 0:
        logger.info("trade_cache_health_watch_disabled", interval_s=interval_s)
        return
    interval_s = max(interval_s, MIN_INTERVAL_S)

    logger.info("trade_cache_health_watch_started", interval_s=interval_s)
    while True:
        try:
            await asyncio.to_thread(probe)
        except Exception as exc:
            # The probe reports its own failures through the metrics and never raises; this is only the thread hop.
            logger.warning("trade_cache_health_watch_failed", error=str(exc))
        await asyncio.sleep(interval_s)
