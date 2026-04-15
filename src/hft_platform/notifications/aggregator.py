"""Alert deduplication and time-window aggregation."""
from __future__ import annotations

from dataclasses import dataclass, field

from hft_platform.notifications.alert import Alert, AlertSeverity


@dataclass(slots=True)
class AggregationSummary:
    """Summary emitted when an aggregation window expires with suppressed alerts."""

    dedup_key: str
    first_alert: Alert
    suppressed_count: int
    window_start_ns: int
    window_end_ns: int


@dataclass(slots=True)
class _AggWindow:
    """Tracks a single dedup_key's aggregation window."""

    first_alert: Alert
    window_start_ns: int
    window_end_ns: int
    count: int = 1


class AlertAggregator:
    """Deduplicates alerts by dedup_key within a configurable time window.

    - First alert per key passes through immediately.
    - Subsequent alerts with the same key within the window are suppressed.
    - FATAL alerts are never aggregated.
    - Alerts with dedup_key=None always pass through.
    """

    __slots__ = ("_window_ns", "_windows")

    def __init__(self, window_ns: int = 300_000_000_000) -> None:
        self._window_ns = window_ns
        self._windows: dict[str, _AggWindow] = {}

    def process(self, alert: Alert) -> Alert | None:
        """Process an incoming alert. Returns the alert if it should be sent, None if suppressed."""
        if alert.dedup_key is None:
            return alert
        if alert.severity == AlertSeverity.FATAL:
            return alert

        key = alert.dedup_key
        window = self._windows.get(key)

        if window is None or alert.ts_ns > window.window_end_ns:
            self._windows[key] = _AggWindow(
                first_alert=alert,
                window_start_ns=alert.ts_ns,
                window_end_ns=alert.ts_ns + self._window_ns,
            )
            return alert

        window.count += 1
        return None

    def flush_expired(self, now_ns: int) -> list[AggregationSummary]:
        """Flush windows that have expired, returning summaries for those with suppressed alerts."""
        expired: list[AggregationSummary] = []
        to_remove: list[str] = []

        for key, window in self._windows.items():
            if now_ns > window.window_end_ns and window.count > 1:
                expired.append(
                    AggregationSummary(
                        dedup_key=key,
                        first_alert=window.first_alert,
                        suppressed_count=window.count - 1,
                        window_start_ns=window.window_start_ns,
                        window_end_ns=window.window_end_ns,
                    )
                )
            if now_ns > window.window_end_ns:
                to_remove.append(key)

        for key in to_remove:
            del self._windows[key]

        return expired
