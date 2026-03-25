"""Orphan order detector — identifies and reports stale broker orders.

Periodically queries the broker for open orders and classifies them as
orphaned if they have been pending beyond a configurable staleness threshold.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("order.orphan_detector")


class OrphanDetector:
    """Detects and reports stale/orphaned orders on the broker side.

    An order is considered *stale* if it has been open for longer than
    ``stale_threshold_s`` seconds.
    """

    __slots__ = (
        "_broker_client",
        "_stale_threshold_s",
        "_check_interval_s",
        "_enabled",
        "_running",
        "_on_orphan",
        "_orphan_count",
    )

    def __init__(
        self,
        broker_client: Any,
        *,
        stale_threshold_s: float = 60.0,
        check_interval_s: float = 30.0,
        on_orphan: Callable[[list[dict[str, Any]]], Any] | None = None,
    ) -> None:
        self._broker_client = broker_client
        self._stale_threshold_s = stale_threshold_s
        self._check_interval_s = check_interval_s
        self._enabled: bool = True
        self._running: bool = False
        self._on_orphan = on_orphan
        self._orphan_count: int = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def orphan_count(self) -> int:
        return self._orphan_count

    def enable(self) -> None:
        self._enabled = True
        logger.info("orphan_detector_enabled")

    def disable(self) -> None:
        self._enabled = False
        logger.info("orphan_detector_disabled")

    def _classify(self, orders: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Classify orders into (stale, active) based on age.

        Returns:
            Tuple of (stale_orders, active_orders).
        """
        now_ns = timebase.now_ns()
        threshold_ns = int(self._stale_threshold_s * 1_000_000_000)
        stale: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        for order in orders:
            created_ns = int(order.get("created_ns", 0) or 0)
            age_ns = now_ns - created_ns if created_ns > 0 else 0
            if age_ns > threshold_ns and created_ns > 0:
                stale.append(order)
            else:
                active.append(order)
        return stale, active

    def _find_stale(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return only the stale orders from a list."""
        stale, _ = self._classify(orders)
        return stale

    async def check_once(self) -> list[dict[str, Any]]:
        """Run a single orphan check cycle.

        Returns:
            List of stale/orphaned orders found.
        """
        if not self._enabled:
            return []

        try:
            open_orders = await asyncio.to_thread(self._broker_client.list_open_orders)
        except Exception as exc:
            logger.error("orphan_detector_query_failed", error=str(exc))
            return []

        stale = self._find_stale(open_orders)
        if stale:
            self._orphan_count += len(stale)
            logger.warning("orphan_orders_detected", count=len(stale))
            if self._on_orphan is not None:
                try:
                    self._on_orphan(stale)
                except Exception as exc:
                    logger.error("orphan_callback_error", error=str(exc))
        return stale

    async def run(self) -> None:
        """Periodically check for orphan orders until stopped."""
        self._running = True
        logger.info(
            "orphan_detector_started",
            interval_s=self._check_interval_s,
            threshold_s=self._stale_threshold_s,
        )
        try:
            while self._running:
                await asyncio.sleep(self._check_interval_s)
                if self._enabled:
                    await self.check_once()
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
