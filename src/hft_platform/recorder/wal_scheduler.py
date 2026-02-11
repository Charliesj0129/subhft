"""WAL Scheduler for scheduled batch imports at market close.

Triggers WAL batch flush after market close to ensure all intraday
data is persisted to the database.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from typing import TYPE_CHECKING

from structlog import get_logger

from hft_platform.core.market_calendar import get_calendar
from hft_platform.observability.metrics import MetricsRegistry

if TYPE_CHECKING:
    from hft_platform.recorder.loader import WALLoaderService

logger = get_logger("recorder.wal_scheduler")


class WALScheduler:
    """Schedule WAL batch import at market close.

    Monitors market close time and triggers a force flush of all
    pending WAL files shortly after market close.
    """

    def __init__(self, loader: WALLoaderService):
        """Initialize WAL scheduler.

        Args:
            loader: WALLoaderService instance to flush
        """
        self._loader = loader
        self._calendar = get_calendar()
        self._close_buffer_s = float(os.getenv("HFT_WAL_CLOSE_BUFFER_S", "300"))  # 5 min after close
        self._check_interval_s = float(os.getenv("HFT_WAL_SCHEDULER_INTERVAL_S", "60"))  # Check every minute
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_flush_date: dt.date | None = None
        self._metrics = MetricsRegistry.get()

        # Retry configuration (O2)
        self._flush_max_retries = int(os.getenv("HFT_WAL_FLUSH_MAX_RETRIES", "3"))
        self._flush_base_delay_s = float(os.getenv("HFT_WAL_FLUSH_BASE_DELAY_S", "1.0"))
        self._flush_max_delay_s = float(os.getenv("HFT_WAL_FLUSH_MAX_DELAY_S", "30.0"))

    @property
    def running(self) -> bool:
        """Check if scheduler is running."""
        return self._running

    def start(self) -> None:
        """Start scheduler thread."""
        if self._running:
            logger.debug("WAL scheduler already running")
            return

        enabled = os.getenv("HFT_WAL_SCHEDULER_ENABLED", "1")
        if enabled.lower() in {"0", "false", "no", "off"}:
            logger.info("WAL scheduler disabled by HFT_WAL_SCHEDULER_ENABLED")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._schedule_loop,
            name="wal-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "WAL scheduler started",
            close_buffer_s=self._close_buffer_s,
            check_interval_s=self._check_interval_s,
        )

    def stop(self) -> None:
        """Stop scheduler thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            logger.info("WAL scheduler stopped")
        self._thread = None

    def _get_check_interval(self) -> float:
        """Get adaptive check interval based on market state.

        Returns different intervals to optimize CPU usage:
        - Trading hours: check every minute (responsive)
        - Pre-market (1 hour before): check every 5 minutes
        - Post-close buffer: check every minute (for flush trigger)
        - Non-trading time: check every hour (minimal CPU)

        Returns:
            Check interval in seconds
        """
        now = dt.datetime.now(self._calendar._tz)
        today = now.date()

        # Non-trading day: check every hour
        if not self._calendar.is_trading_day(today):
            return 3600.0

        open_time = self._calendar.get_session_open(today)
        close_time = self._calendar.get_session_close(today)

        if open_time is None or close_time is None:
            return 3600.0

        # Pre-market (1 hour before open): check every 5 minutes
        pre_market = open_time - dt.timedelta(hours=1)
        if pre_market <= now < open_time:
            return 300.0

        # Trading hours: check every minute
        if open_time <= now <= close_time:
            return 60.0

        # Post-close buffer period: check every minute (need to catch flush trigger)
        buffer_end = close_time + dt.timedelta(seconds=self._close_buffer_s + 300)
        if close_time < now <= buffer_end:
            return 60.0

        # After buffer: check every hour
        return 3600.0

    def _schedule_loop(self) -> None:
        """Main scheduling loop - check periodically for market close.

        Uses adaptive intervals to reduce CPU usage during non-trading hours.
        """
        while self._running:
            try:
                self._check_and_flush()
            except Exception as exc:
                logger.error("WAL scheduler error", error=str(exc))

            # Get adaptive interval based on market state
            interval = self._get_check_interval()

            # Sleep in short intervals to allow clean shutdown
            for _ in range(int(interval)):
                if not self._running:
                    break
                time.sleep(1.0)

    def _check_and_flush(self) -> None:
        """Check if we should trigger batch flush."""
        now = dt.datetime.now(self._calendar._tz)
        today = now.date()

        # Skip if not a trading day
        if not self._calendar.is_trading_day(today):
            return

        # Skip if already flushed today
        if self._last_flush_date == today:
            return

        # Get today's close time + buffer
        close_time = self._calendar.get_session_close(today)
        if close_time is None:
            return

        trigger_time = close_time + dt.timedelta(seconds=self._close_buffer_s)

        # Check if we should flush
        if now >= trigger_time:
            logger.info(
                "Market closed, triggering WAL batch flush",
                close_time=close_time.isoformat(),
                trigger_time=trigger_time.isoformat(),
                now=now.isoformat(),
            )
            self._do_batch_flush()
            self._last_flush_date = today

    def _do_batch_flush(self) -> bool:
        """Force process all pending WAL files with exponential backoff retry.

        Returns:
            True if flush succeeded, False if all retries exhausted
        """
        last_error: Exception | None = None

        for attempt in range(self._flush_max_retries):
            try:
                start_ns = time.perf_counter_ns()
                # Force immediate processing (ignore 2-second mtime check)
                self._loader.process_files(force=True)
                duration_ms = (time.perf_counter_ns() - start_ns) / 1e6

                logger.info(
                    "WAL batch flush completed",
                    attempt=attempt + 1,
                    duration_ms=round(duration_ms, 2),
                )
                if self._metrics:
                    self._metrics.wal_batch_flush_total.labels(result="ok").inc()
                return True

            except Exception as exc:
                last_error = exc
                if attempt < self._flush_max_retries - 1:
                    delay = min(
                        self._flush_base_delay_s * (2**attempt),
                        self._flush_max_delay_s,
                    )
                    logger.warning(
                        "WAL batch flush failed, retrying",
                        attempt=attempt + 1,
                        max_retries=self._flush_max_retries,
                        delay_s=delay,
                        error=str(exc),
                    )
                    if self._metrics:
                        self._metrics.wal_batch_flush_retry_total.inc()
                    time.sleep(delay)

        logger.error(
            "WAL batch flush failed after max retries",
            max_retries=self._flush_max_retries,
            error=str(last_error),
        )
        if self._metrics:
            self._metrics.wal_batch_flush_total.labels(result="error").inc()
        return False

    def trigger_flush(self) -> bool:
        """Manually trigger a batch flush.

        Returns:
            True if flush succeeded (possibly after retries)
        """
        return self._do_batch_flush()
