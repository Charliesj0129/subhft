"""Alert data models for the tiered notification routing system."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class AlertSeverity(enum.IntEnum):
    """Alert severity levels for routing decisions."""

    INFO = 0
    WARN = 1
    CRITICAL = 2
    FATAL = 3


@dataclass(slots=True, frozen=True)
class Alert:
    """Immutable alert message for the notification pipeline."""

    alert_id: str
    severity: AlertSeverity
    category: str
    source: str
    title: str
    detail: str
    ts_ns: int
    dedup_key: str | None
    metadata: dict | None


@dataclass(slots=True)
class SilenceRule:
    """Rule to suppress alerts matching specific criteria within a time window."""

    rule_id: str
    category: str | None
    source: str | None
    severity_max: AlertSeverity
    start_ns: int
    end_ns: int
    reason: str

    def matches(self, alert: Alert) -> bool:
        """Return True if this rule silences the given alert."""
        if self.category is not None and alert.category != self.category:
            return False
        if self.source is not None and alert.source != self.source:
            return False
        if alert.severity > self.severity_max:
            return False
        if alert.ts_ns < self.start_ns:
            return False
        if self.end_ns != 0 and alert.ts_ns > self.end_ns:
            return False
        return True
