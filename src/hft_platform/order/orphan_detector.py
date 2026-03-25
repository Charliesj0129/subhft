"""OrphanDetector — identifies stale or orphaned orders.

Classifies open orders as "active", "stale", or "orphan" based on age
thresholds and compares against the local order tracker to find orders
that the platform no longer manages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from hft_platform.core import timebase

logger = structlog.get_logger("order.orphan_detector")


@dataclass(slots=True, frozen=True)
class OrphanClassification:
    """Classification result for a single order."""

    order_id: str
    symbol: str
    status: str  # "active", "stale", "orphan"
    age_ns: int


class OrphanDetector:
    """Detects stale and orphaned orders by comparing broker state to local tracker.

    An order is *stale* if it has been open longer than ``stale_threshold_ns``.
    An order is *orphan* if it appears in the broker's open-order list but is
    absent from the local order tracker.
    """

    __slots__ = (
        "_stale_threshold_ns",
        "_local_tracker",
        "_enabled",
    )

    def __init__(
        self,
        stale_threshold_ns: int = 60_000_000_000,  # 60 seconds
        local_tracker: Any | None = None,
    ) -> None:
        self._stale_threshold_ns = stale_threshold_ns
        self._local_tracker = local_tracker
        self._enabled: bool = True

    @property
    def enabled(self) -> bool:
        """Whether the detector is active."""
        return self._enabled

    def enable(self) -> None:
        """Enable the orphan detector."""
        self._enabled = True

    def disable(self) -> None:
        """Disable the orphan detector."""
        self._enabled = False

    def classify(self, broker_orders: list[dict[str, Any]]) -> list[OrphanClassification]:
        """Classify a list of broker open orders.

        Args:
            broker_orders: List of dicts with at minimum ``order_id``,
                ``symbol``, and ``timestamp_ns`` keys.

        Returns:
            List of classification results.
        """
        if not self._enabled:
            return []

        now_ns = timebase.now_ns()
        results: list[OrphanClassification] = []
        local_ids = self._get_local_order_ids()

        for order in broker_orders:
            order_id = str(order.get("order_id", ""))
            symbol = str(order.get("symbol", ""))
            ts_ns = int(order.get("timestamp_ns", 0))
            age_ns = now_ns - ts_ns if ts_ns > 0 else 0

            if local_ids is not None and order_id and order_id not in local_ids:
                status = "orphan"
            elif age_ns > self._stale_threshold_ns:
                status = "stale"
            else:
                status = "active"

            results.append(
                OrphanClassification(
                    order_id=order_id,
                    symbol=symbol,
                    status=status,
                    age_ns=age_ns,
                )
            )

        return results

    def find_stale(self, broker_orders: list[dict[str, Any]]) -> list[OrphanClassification]:
        """Return only stale and orphan orders."""
        return [c for c in self.classify(broker_orders) if c.status in ("stale", "orphan")]

    def _get_local_order_ids(self) -> set[str] | None:
        """Extract tracked order IDs from the local tracker.

        Returns ``None`` when no tracker is configured (skip orphan detection).
        """
        if self._local_tracker is None:
            return None
        if hasattr(self._local_tracker, "active_order_ids"):
            return set(self._local_tracker.active_order_ids())
        if hasattr(self._local_tracker, "orders"):
            return set(str(k) for k in self._local_tracker.orders)
        return set()
