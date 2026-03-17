"""Quote runtime protocol and shared watchdog base.

Captures the shared patterns observed in both Shioaji ``QuoteRuntime`` and
Fubon ``FubonQuoteRuntime``:

* ``QuoteRuntimeProtocol`` — the minimal interface every quote runtime must
  satisfy (callback registration, subscribe/unsubscribe, watchdog, stop).
* ``BaseQuoteWatchdog`` — reusable timeout-based watchdog that detects quote
  feed stalls.  Both brokers implement this pattern with a background thread
  that checks elapsed time since the last data arrival.

This is an ADDITIVE extraction — existing implementations are not modified.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger("feed_adapter._base.quote_runtime")


@runtime_checkable
class QuoteRuntimeProtocol(Protocol):
    """Minimal interface for broker quote runtimes.

    Both Shioaji and Fubon quote runtimes expose these operations,
    though with broker-specific internal implementations.  This protocol
    enables broker-agnostic code to interact with any quote runtime.
    """

    def register_quote_callbacks(
        self,
        on_tick: Callable[..., Any],
        on_bidask: Callable[..., Any],
    ) -> None:
        """Register canonical tick and bidask callbacks."""
        ...

    def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to market data for the given symbols."""
        ...

    def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from market data for the given symbols."""
        ...

    def start_quote_watchdog(self, timeout_s: float = 30.0) -> None:
        """Start a watchdog that monitors data freshness."""
        ...

    def stop(self) -> None:
        """Stop the quote runtime and release resources."""
        ...


class BaseQuoteWatchdog:
    """Reusable timeout-based watchdog for quote feed stall detection.

    Both Shioaji and Fubon use a background thread that periodically checks
    whether new data has arrived within a configurable timeout.  This base
    class captures that shared pattern.

    Usage::

        watchdog = BaseQuoteWatchdog(timeout_s=30.0, on_stall=my_handler)
        watchdog.start()

        # On each data arrival:
        watchdog.notify_data()

        # Shutdown:
        watchdog.stop()
    """

    __slots__ = (
        "_timeout_s",
        "_on_stall",
        "_thread",
        "_running",
        "_last_data_ts",
        "_check_interval_s",
    )

    def __init__(
        self,
        timeout_s: float = 30.0,
        on_stall: Callable[[float], None] | None = None,
        check_interval_s: float | None = None,
    ) -> None:
        """Initialise the watchdog.

        Args:
            timeout_s: Seconds of silence before declaring a stall.
            on_stall: Callback invoked with the gap (seconds) when a stall
                is detected.  If ``None``, a warning is logged.
            check_interval_s: How often the watchdog checks for stalls.
                Defaults to ``timeout_s`` (check once per timeout window).
        """
        self._timeout_s: float = timeout_s
        self._on_stall: Callable[[float], None] | None = on_stall
        self._thread: threading.Thread | None = None
        self._running: bool = False
        self._last_data_ts: float = 0.0
        self._check_interval_s: float = check_interval_s if check_interval_s is not None else timeout_s

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def notify_data(self) -> None:
        """Record that data has arrived (call from the data callback)."""
        self._last_data_ts = time.monotonic()

    def start(self) -> None:
        """Start the watchdog background thread.

        Idempotent: does nothing if already running.
        """
        if self._running:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        logger.info(
            "quote_watchdog_start",
            timeout_s=self._timeout_s,
            check_interval_s=self._check_interval_s,
        )

        self._thread = threading.Thread(
            target=self._watch_loop,
            name="base-quote-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog thread.

        Blocks up to 5 seconds for the thread to finish.
        """
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("quote_watchdog_stopped")

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the watchdog thread is active."""
        return self._running

    @property
    def last_data_ts(self) -> float:
        """Monotonic timestamp of the last data notification."""
        return self._last_data_ts

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _watch_loop(self) -> None:
        """Background loop that checks for data freshness."""
        try:
            while self._running:
                time.sleep(self._check_interval_s)
                if not self._running:
                    break
                last = self._last_data_ts
                if last <= 0:
                    continue
                gap = time.monotonic() - last
                if gap >= self._timeout_s:
                    self._handle_stall(gap)
        except Exception as exc:
            logger.error("quote_watchdog_crashed", error=str(exc))
        finally:
            self._running = False

    def _handle_stall(self, gap_s: float) -> None:
        """Invoked when a stall is detected."""
        if self._on_stall is not None:
            self._on_stall(gap_s)
        else:
            logger.warning(
                "quote_watchdog_stall",
                gap_s=round(gap_s, 3),
                timeout_s=self._timeout_s,
            )
