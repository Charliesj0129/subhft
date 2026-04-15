"""HealingOrchestrator — core fault-to-repair execution engine."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

import structlog

from hft_platform.core import timebase
from hft_platform.healing.actions import ActionRegistry
from hft_platform.healing.fault import FaultEvent, RiskLevel
from hft_platform.healing.playbook import HealingPlaybook
from hft_platform.notifications.alert import Alert, AlertSeverity

logger = structlog.get_logger("healing.orchestrator")


def _make_id() -> str:
    return str(uuid.uuid4())[:8]


@dataclass(slots=True)
class HealingResult:
    fault_id: str
    playbook_name: str
    success: bool
    actions_completed: int
    actions_total: int
    error: str | None = None
    pending_approval: bool = False
    duration_ms: float = 0.0


class HealingOrchestrator:
    """Processes FaultEvents: finds matching playbook, executes repair actions in sequence.

    AUTO-risk actions execute immediately.
    CONFIRM-risk actions emit a CRITICAL alert and pause, setting pending_approval=True.
    """

    __slots__ = ("_playbook", "_registry", "_alert_callback", "_pending_faults")

    def __init__(
        self,
        playbook: HealingPlaybook,
        action_registry: ActionRegistry,
        alert_callback: Callable[[Alert], Awaitable[None]],
    ) -> None:
        self._playbook = playbook
        self._registry = action_registry
        self._alert_callback = alert_callback
        self._pending_faults: dict[str, tuple[FaultEvent, str, int]] = {}

    async def handle_fault(self, fault: FaultEvent) -> HealingResult | None:
        """Process a fault event. Returns HealingResult on match, None if no playbook found."""
        import time

        start = time.monotonic()

        entry = self._playbook.find_match(fault)
        if entry is None:
            await self._alert_callback(Alert(
                alert_id=_make_id(),
                severity=AlertSeverity.WARN,
                category=fault.category.value,
                source="healing_orchestrator",
                title=f"No playbook for fault: {fault.description}",
                detail=(
                    f"Fault {fault.fault_id} ({fault.category.value}) has no matching playbook."
                ),
                ts_ns=timebase.now_ns(),
                dedup_key=f"no_playbook:{fault.category.value}",
                metadata={"fault_id": fault.fault_id},
            ))
            return None

        logger.info(
            "healing_orchestrator.executing",
            fault_id=fault.fault_id,
            playbook=entry.name,
            actions=len(entry.actions),
        )

        completed = 0
        for i, action_def in enumerate(entry.actions):
            if action_def.risk == RiskLevel.CONFIRM:
                await self._alert_callback(Alert(
                    alert_id=_make_id(),
                    severity=AlertSeverity.CRITICAL,
                    category=fault.category.value,
                    source="healing_orchestrator",
                    title=f"Approval needed: {action_def.name}",
                    detail=(
                        f"Fault {fault.fault_id}: playbook '{entry.name}' step {i + 1} "
                        f"requires /approve {fault.fault_id}"
                    ),
                    ts_ns=timebase.now_ns(),
                    dedup_key=f"confirm:{fault.fault_id}",
                    metadata={"fault_id": fault.fault_id, "action": action_def.name},
                ))
                self._pending_faults[fault.fault_id] = (fault, entry.name, i)
                self._playbook.mark_used(entry.name, fault.ts_ns)
                elapsed = (time.monotonic() - start) * 1000
                return HealingResult(
                    fault_id=fault.fault_id,
                    playbook_name=entry.name,
                    success=False,
                    actions_completed=completed,
                    actions_total=len(entry.actions),
                    pending_approval=True,
                    duration_ms=elapsed,
                )

            action_fn = self._registry.get(action_def.name)
            if action_fn is None:
                logger.warning("healing_orchestrator.action_not_found", action=action_def.name)
                continue

            try:
                await asyncio.wait_for(action_fn(**action_def.params), timeout=action_def.timeout_s)
                completed += 1
            except Exception as exc:
                elapsed = (time.monotonic() - start) * 1000
                logger.error(
                    "healing_orchestrator.action_failed",
                    action=action_def.name,
                    error=str(exc),
                )
                await self._alert_callback(Alert(
                    alert_id=_make_id(),
                    severity=AlertSeverity.FATAL,
                    category=fault.category.value,
                    source="healing_orchestrator",
                    title=f"Healing failed: {action_def.name}",
                    detail=(
                        f"Fault {fault.fault_id}: action '{action_def.name}' failed: {exc}"
                    ),
                    ts_ns=timebase.now_ns(),
                    dedup_key=f"healing_fail:{fault.fault_id}",
                    metadata={
                        "fault_id": fault.fault_id,
                        "action": action_def.name,
                        "error": str(exc),
                    },
                ))
                self._playbook.mark_used(entry.name, fault.ts_ns)
                return HealingResult(
                    fault_id=fault.fault_id,
                    playbook_name=entry.name,
                    success=False,
                    actions_completed=completed,
                    actions_total=len(entry.actions),
                    error=str(exc),
                    duration_ms=elapsed,
                )

        self._playbook.mark_used(entry.name, fault.ts_ns)
        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "healing_orchestrator.completed",
            fault_id=fault.fault_id,
            playbook=entry.name,
            actions_completed=completed,
            duration_ms=round(elapsed, 1),
        )
        return HealingResult(
            fault_id=fault.fault_id,
            playbook_name=entry.name,
            success=True,
            actions_completed=completed,
            actions_total=len(entry.actions),
            duration_ms=elapsed,
        )

    def approve(self, fault_id: str) -> bool:
        """Acknowledge and clear a pending CONFIRM-risk action. Returns True if found."""
        return self._pending_faults.pop(fault_id, None) is not None

    def reject(self, fault_id: str) -> bool:
        """Reject and clear a pending CONFIRM-risk action. Returns True if found."""
        return self._pending_faults.pop(fault_id, None) is not None
