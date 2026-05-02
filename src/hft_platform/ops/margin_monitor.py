"""MarginMonitor -- monitors broker margin utilization and triggers alerts.

Polls broker's get_margin() API at configurable intervals.
Thresholds:
  - warn_ratio (default 0.80): notify once per transition
  - critical_ratio (default 0.90): enter reduce-only + notify

Config env vars:
  HFT_MARGIN_WARN_RATIO     (default 0.80)
  HFT_MARGIN_CRITICAL_RATIO (default 0.90)
  HFT_MARGIN_POLL_INTERVAL_S (default 30)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True, frozen=True)
class MarginCheckResult:
    """Result of a single margin check."""

    ratio: float  # margin_used / margin_available (0.0-1.0+)
    action: str  # "ok" | "warn" | "critical" | "error"
    margin_used: int  # NTD (not scaled)
    margin_available: int  # NTD


class MarginMonitor:
    """Polls broker margin API and classifies utilization level.

    Tracks state transitions (ok/warn/critical) and only logs once
    per transition to avoid log spam.  Float is used for ratio
    calculations -- this is monitoring, not accounting.
    """

    __slots__ = (
        "_broker_client",
        "_warn_ratio",
        "_critical_ratio",
        "_poll_interval_ns",
        "_last_poll_ns",
        "_in_warning",
        "_in_critical",
    )

    def __init__(
        self,
        broker_client: Any,
        *,
        warn_ratio: float = 0.80,
        critical_ratio: float = 0.90,
        poll_interval_s: int = 30,
    ) -> None:
        self._broker_client = broker_client
        self._warn_ratio = warn_ratio
        self._critical_ratio = critical_ratio
        self._poll_interval_ns = poll_interval_s * 1_000_000_000
        self._last_poll_ns = 0
        self._in_warning = False
        self._in_critical = False

    async def check(self, now_ns: int) -> MarginCheckResult | None:
        """Check margin if poll interval has elapsed.

        Returns:
            MarginCheckResult with action classification, or None if
            the poll interval has not elapsed yet.
        """
        if now_ns - self._last_poll_ns < self._poll_interval_ns:
            return None

        self._last_poll_ns = now_ns

        try:
            margin_data = await asyncio.to_thread(self._broker_client.get_margin)
        except Exception as exc:
            logger.warning("margin_monitor.poll_failed", error=str(exc))
            return MarginCheckResult(ratio=0.0, action="error", margin_used=0, margin_available=0)

        # Extract margin values from broker response
        if isinstance(margin_data, dict):
            used = int(margin_data.get("margin_used", margin_data.get("equity_used", 0)) or 0)
            available = int(margin_data.get("margin_available", margin_data.get("equity", 1)) or 1)
        else:
            used = int(getattr(margin_data, "margin_used", 0))
            available = int(getattr(margin_data, "margin_available", 1))

        if available <= 0:
            logger.warning(
                "margin_monitor.zero_or_negative_available",
                raw_available=available,
                used=used,
            )
            available = 1  # Prevent division by zero

        ratio = used / available  # float OK -- monitoring, not accounting

        if ratio >= self._critical_ratio:
            action = "critical"
            if not self._in_critical:
                self._in_critical = True
                self._in_warning = True
                logger.error(
                    "margin_monitor.critical",
                    ratio=f"{ratio:.2%}",
                    used=used,
                    available=available,
                )
        elif ratio >= self._warn_ratio:
            action = "warn"
            if not self._in_warning:
                self._in_warning = True
                logger.warning(
                    "margin_monitor.warning",
                    ratio=f"{ratio:.2%}",
                    used=used,
                    available=available,
                )
            if self._in_critical:
                self._in_critical = False  # Recovered from critical
        else:
            action = "ok"
            if self._in_warning or self._in_critical:
                self._in_warning = False
                self._in_critical = False
                logger.info("margin_monitor.recovered", ratio=f"{ratio:.2%}")

        return MarginCheckResult(
            ratio=ratio,
            action=action,
            margin_used=used,
            margin_available=available,
        )
