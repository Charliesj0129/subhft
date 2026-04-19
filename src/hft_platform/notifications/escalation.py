"""Escalation chain for unacknowledged CRITICAL/FATAL alerts."""

from __future__ import annotations

from dataclasses import dataclass

from hft_platform.notifications.alert import Alert, AlertSeverity


@dataclass(slots=True)
class _EscalationEntry:
    """Tracks escalation state for a single alert."""

    alert: Alert
    tracked_ns: int
    escalation_count: int = 0
    last_escalated_ns: int = 0


class EscalationTracker:
    """Tracks CRITICAL/FATAL alerts and generates escalation events.

    Only CRITICAL and FATAL alerts are tracked. INFO/WARN are ignored.
    Escalation intervals define how long to wait before each resend.
    After max_escalations, the alert is no longer escalated (but stays tracked
    until acknowledged).
    """

    __slots__ = ("_intervals_ns", "_max_escalations", "_entries")

    def __init__(
        self,
        intervals_ns: list[int] | None = None,
        max_escalations: int = 3,
    ) -> None:
        self._intervals_ns: list[int] = intervals_ns or [
            300_000_000_000,
            900_000_000_000,
        ]
        self._max_escalations = max_escalations
        self._entries: dict[str, _EscalationEntry] = {}

    def track(self, alert: Alert) -> None:
        """Start tracking an alert for escalation. Only CRITICAL/FATAL are tracked."""
        if alert.severity < AlertSeverity.CRITICAL:
            return
        self._entries[alert.alert_id] = _EscalationEntry(
            alert=alert,
            tracked_ns=alert.ts_ns,
        )

    def acknowledge(self, alert_id: str) -> None:
        """Acknowledge an alert, stopping its escalation chain."""
        self._entries.pop(alert_id, None)

    def is_tracked(self, alert_id: str) -> bool:
        """Check if an alert is being tracked."""
        return alert_id in self._entries

    def get_due(self, now_ns: int) -> list[Alert]:
        """Return alerts whose next escalation is due."""
        due: list[Alert] = []
        for entry in self._entries.values():
            if entry.escalation_count >= self._max_escalations:
                continue
            interval_idx = min(entry.escalation_count, len(self._intervals_ns) - 1)
            interval = self._intervals_ns[interval_idx]
            reference_ns = entry.last_escalated_ns if entry.last_escalated_ns else entry.tracked_ns
            if now_ns - reference_ns >= interval:
                due.append(entry.alert)
        return due

    def mark_escalated(self, alert_id: str, now_ns: int) -> None:
        """Mark an alert as having been escalated at the given time."""
        entry = self._entries.get(alert_id)
        if entry is not None:
            entry.escalation_count += 1
            entry.last_escalated_ns = now_ns
