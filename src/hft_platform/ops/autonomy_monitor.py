"""Autonomy monitor — watches platform health signals and triggers degradation.

Periodically evaluates health inputs (feed gaps, memory, persistence, broker
connectivity) and transitions the autonomy state machine accordingly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.ops.autonomy import AutonomyMode, AutonomyTransition
from hft_platform.ops.evidence import AutonomyEvidenceWriter

logger = get_logger("ops.autonomy_monitor")

_MODE_SEVERITY: dict[AutonomyMode, int] = {
    AutonomyMode.NORMAL: 0,
    AutonomyMode.STRATEGY_QUARANTINED: 1,
    AutonomyMode.PLATFORM_REDUCE_ONLY: 2,
    AutonomyMode.HALT: 3,
}


def _mode_gt(a: AutonomyMode, b: AutonomyMode) -> bool:
    return _MODE_SEVERITY.get(a, 0) > _MODE_SEVERITY.get(b, 0)


class HealthSignal(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


@dataclass(slots=True)
class HealthSnapshot:
    """Point-in-time health assessment from various subsystems."""

    feed_gap_s: float = 0.0
    memory_rss_gb: float = 0.0
    memory_limit_gb: float = 4.0
    persistence_healthy: bool = True
    broker_connected: bool = True
    reconciliation_drift_streak: int = 0
    storm_guard_state: str = "NORMAL"
    ts_ns: int = 0


@dataclass(slots=True)
class MonitorConfig:
    """Thresholds for the autonomy monitor."""

    check_interval_s: float = 5.0
    feed_gap_degrade_s: float = 10.0
    feed_gap_halt_s: float = 30.0
    memory_degrade_pct: float = 0.85
    memory_halt_pct: float = 0.95
    drift_streak_degrade: int = 3
    drift_streak_halt: int = 6
    enabled: bool = True


class AutonomyMonitor:
    """Monitors platform health and manages autonomy mode transitions.

    The monitor evaluates a ``HealthSnapshot`` on each tick and decides
    whether to escalate, maintain, or (after manual rearm) de-escalate
    the platform autonomy mode.
    """

    __slots__ = (
        "_mode",
        "_config",
        "_evidence_writer",
        "_on_transition",
        "_running",
        "_last_snapshot",
        "_transition_count",
    )

    def __init__(
        self,
        config: MonitorConfig | None = None,
        evidence_writer: AutonomyEvidenceWriter | None = None,
        on_transition: Callable[[AutonomyTransition], Any] | None = None,
    ) -> None:
        self._mode: AutonomyMode = AutonomyMode.NORMAL
        self._config = config or MonitorConfig()
        self._evidence_writer = evidence_writer
        self._on_transition = on_transition
        self._running: bool = False
        self._last_snapshot: HealthSnapshot | None = None
        self._transition_count: int = 0

    # ---- Properties ----

    @property
    def mode(self) -> AutonomyMode:
        return self._mode

    @property
    def transition_count(self) -> int:
        return self._transition_count

    @property
    def running(self) -> bool:
        return self._running

    # ---- Evaluation ----

    def evaluate(self, snapshot: HealthSnapshot) -> AutonomyMode:
        """Evaluate a health snapshot and transition mode if necessary.

        Returns the (possibly new) autonomy mode.
        """
        self._last_snapshot = snapshot
        target = self._determine_target(snapshot)

        if _mode_gt(target, self._mode):
            self._escalate(target, self._build_reason(snapshot, target))
        return self._mode

    def _determine_target(self, snap: HealthSnapshot) -> AutonomyMode:
        """Determine the target autonomy mode from health signals."""
        # HALT-level checks
        if snap.feed_gap_s >= self._config.feed_gap_halt_s:
            return AutonomyMode.HALT
        if snap.memory_rss_gb >= snap.memory_limit_gb * self._config.memory_halt_pct:
            return AutonomyMode.HALT
        if snap.storm_guard_state == "HALT":
            return AutonomyMode.HALT

        # PLATFORM_REDUCE_ONLY checks
        if snap.feed_gap_s >= self._config.feed_gap_degrade_s:
            return AutonomyMode.PLATFORM_REDUCE_ONLY
        if not snap.persistence_healthy:
            return AutonomyMode.PLATFORM_REDUCE_ONLY
        if not snap.broker_connected:
            return AutonomyMode.PLATFORM_REDUCE_ONLY
        if snap.memory_rss_gb >= snap.memory_limit_gb * self._config.memory_degrade_pct:
            return AutonomyMode.PLATFORM_REDUCE_ONLY
        if snap.reconciliation_drift_streak >= self._config.drift_streak_degrade:
            return AutonomyMode.PLATFORM_REDUCE_ONLY

        return AutonomyMode.NORMAL

    @staticmethod
    def _build_reason(snap: HealthSnapshot, target: AutonomyMode) -> str:
        reasons: list[str] = []
        if snap.feed_gap_s > 0:
            reasons.append(f"feed_gap={snap.feed_gap_s:.1f}s")
        if not snap.broker_connected:
            reasons.append("broker_unavailable")
        if not snap.persistence_healthy:
            reasons.append("persistence_failure")
        if snap.memory_rss_gb > 0:
            reasons.append(f"memory={snap.memory_rss_gb:.2f}GB")
        if snap.reconciliation_drift_streak > 0:
            reasons.append(f"drift_streak={snap.reconciliation_drift_streak}")
        return "; ".join(reasons) if reasons else target.value

    def _escalate(self, target: AutonomyMode, reason: str) -> None:
        old = self._mode
        self._mode = target
        self._transition_count += 1

        transition = AutonomyTransition(
            scope="platform",
            from_mode=old,
            to_mode=target,
            reason=reason,
            manual_rearm_required=True,
        )

        logger.warning(
            "autonomy_escalation",
            from_mode=old.value,
            to_mode=target.value,
            reason=reason,
        )

        if self._evidence_writer is not None:
            try:
                self._evidence_writer.record_transition(
                    scope="platform",
                    mode=target.value,
                    reason=reason,
                    manual_rearm_required=True,
                )
            except Exception:
                pass

        if self._on_transition is not None:
            try:
                self._on_transition(transition)
            except Exception:
                pass

    # ---- Manual rearm ----

    def rearm(self) -> None:
        """Manually reset the autonomy mode back to NORMAL."""
        old = self._mode
        self._mode = AutonomyMode.NORMAL
        logger.info("autonomy_rearmed", from_mode=old.value)

    # ---- Async run loop ----

    async def run(self, snapshot_provider: Callable[[], HealthSnapshot]) -> None:
        """Periodically evaluate health snapshots until stopped."""
        self._running = True
        logger.info("autonomy_monitor_started", interval_s=self._config.check_interval_s)
        try:
            while self._running:
                await asyncio.sleep(self._config.check_interval_s)
                try:
                    snap = snapshot_provider()
                    self.evaluate(snap)
                except Exception as exc:
                    logger.error("autonomy_monitor_eval_error", error=str(exc))
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self._mode.value,
            "transition_count": self._transition_count,
            "running": self._running,
            "last_snapshot": {
                "feed_gap_s": self._last_snapshot.feed_gap_s,
                "broker_connected": self._last_snapshot.broker_connected,
            }
            if self._last_snapshot
            else None,
        }
