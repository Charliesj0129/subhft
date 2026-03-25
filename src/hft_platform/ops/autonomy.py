from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


_ALLOWED_REASON_CODES = frozenset(
    {
        "clickhouse_unhealthy",
        "feed_reconnect_flapping",
        "feed_reconnect_pending",
        "feed_reconnect_unhealthy",
        "manual_operator",
        "queue_depth_exceeded",
        "reconciliation_drift",
        "redis_unhealthy",
        "rss_unhealthy",
        "strategy_exception",
        "strategy_reject_spike",
        "unknown",
        "wal_backlog_unhealthy",
        "broker_unavailable",
        "feed_gap_majority",
        "memory_pressure",
        "persistence_failure",
        "session_close_only",
    }
)
_ALLOWED_SCOPE_CODES = frozenset({"platform", "strategy", "unknown"})


def _normalize_metric_code(value: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


def _reason_code_for_metrics(reason: str) -> str:
    normalized = _normalize_metric_code(reason)
    if normalized in _ALLOWED_REASON_CODES:
        return normalized
    return "unknown"


def _scope_code_for_metrics(scope: str) -> str:
    normalized = _normalize_metric_code(scope)
    if normalized in _ALLOWED_SCOPE_CODES:
        return normalized
    return "unknown"


class AutonomyMode(StrEnum):
    NORMAL = "NORMAL"
    STRATEGY_QUARANTINED = "STRATEGY_QUARANTINED"
    PLATFORM_REDUCE_ONLY = "PLATFORM_REDUCE_ONLY"
    HALT = "HALT"


@dataclass(slots=True, frozen=True)
class AutonomyTransition:
    scope: str
    from_mode: AutonomyMode
    to_mode: AutonomyMode
    reason: str
    manual_rearm_required: bool = True

    @classmethod
    def enter_platform_reduce_only(
        cls,
        reason: str,
        *,
        scope: str = "platform",
        from_mode: AutonomyMode = AutonomyMode.NORMAL,
        manual_rearm_required: bool = True,
    ) -> "AutonomyTransition":
        return cls(
            scope=scope,
            from_mode=from_mode,
            to_mode=AutonomyMode.PLATFORM_REDUCE_ONLY,
            reason=reason,
            manual_rearm_required=manual_rearm_required,
        )

    @property
    def metric_reason(self) -> str:
        return _reason_code_for_metrics(self.reason)

    @property
    def metric_scope(self) -> str:
        return _scope_code_for_metrics(self.scope)

    def metric_labels(self) -> dict[str, str]:
        return {
            "scope": self.metric_scope,
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "reason": self.metric_reason,
        }

    def record_transition(self, metrics: Any) -> None:
        metrics.autonomy_transitions_total.labels(**self.metric_labels()).inc()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "reason": self.reason,
            "manual_rearm_required": self.manual_rearm_required,
        }
