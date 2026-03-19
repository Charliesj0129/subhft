"""Incident mitigation actions — conservative, safety-gated responses.

All actions are **conservative**: reconnect, mode-switch, cancel, log.
Never auto-place orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.core import timebase


@dataclass(frozen=True)
class ActionResult:
    """Outcome of an executed mitigation action."""

    success: bool
    message: str
    timestamp_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "message": self.message, "timestamp_ns": self.timestamp_ns}


@dataclass(frozen=True)
class MitigationAction:
    """Describes a mitigation action to take."""

    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    safe: bool = True
    reason: str = ""
    timestamp_ns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "params": self.params,
            "safe": self.safe,
            "reason": self.reason,
            "timestamp_ns": self.timestamp_ns,
        }


class BaseAction:
    """Abstract base for mitigation actions."""

    action_type: str = "base"

    def is_safe(self) -> bool:
        return True

    def execute(self, context: dict[str, Any] | None = None) -> ActionResult:
        raise NotImplementedError


class NoOpAction(BaseAction):
    """No-operation action for unknown or unhandled alerts."""

    action_type = "noop"

    def __init__(self, reason: str = "unknown alert") -> None:
        self.reason = reason

    def execute(self, context: dict[str, Any] | None = None) -> ActionResult:
        return ActionResult(
            success=True,
            message=f"NoOp: {self.reason}",
            timestamp_ns=timebase.now_ns(),
        )


class ReconnectBrokerAction(BaseAction):
    """Attempt to reconnect broker feed adapter."""

    action_type = "reconnect_broker"

    def __init__(self, max_retries: int = 3, backoff_base_s: float = 2.0) -> None:
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s

    def is_safe(self) -> bool:
        return True

    def execute(self, context: dict[str, Any] | None = None) -> ActionResult:
        return ActionResult(
            success=True,
            message=f"ReconnectBroker: max_retries={self.max_retries}, backoff={self.backoff_base_s}s",
            timestamp_ns=timebase.now_ns(),
        )


class SwitchRecorderModeAction(BaseAction):
    """Switch recorder to WAL-first mode on ClickHouse failure."""

    action_type = "switch_recorder_mode"

    def __init__(self, target_mode: str = "wal_first") -> None:
        self.target_mode = target_mode

    def is_safe(self) -> bool:
        return True

    def execute(self, context: dict[str, Any] | None = None) -> ActionResult:
        return ActionResult(
            success=True,
            message=f"SwitchRecorderMode: target={self.target_mode}",
            timestamp_ns=timebase.now_ns(),
        )


class LogAndEscalateAction(BaseAction):
    """Log critical event and escalate to ops."""

    action_type = "log_and_escalate"

    def __init__(self, severity: str = "critical") -> None:
        self.severity = severity

    def is_safe(self) -> bool:
        return True

    def execute(self, context: dict[str, Any] | None = None) -> ActionResult:
        return ActionResult(
            success=True,
            message=f"LogAndEscalate: severity={self.severity}",
            timestamp_ns=timebase.now_ns(),
        )


class CancelOpenOrdersAction(BaseAction):
    """Cancel all open orders — read-only safety: only cancels, never places."""

    action_type = "cancel_open_orders"

    def is_safe(self) -> bool:
        return True  # Cancellation is always safe

    def execute(self, context: dict[str, Any] | None = None) -> ActionResult:
        return ActionResult(
            success=True,
            message="CancelOpenOrders: cancellation requested",
            timestamp_ns=timebase.now_ns(),
        )


class RestartServiceAction(BaseAction):
    """Restart a specific service."""

    action_type = "restart_service"

    def __init__(self, service: str = "") -> None:
        self.service = service

    def is_safe(self) -> bool:
        return True

    def execute(self, context: dict[str, Any] | None = None) -> ActionResult:
        return ActionResult(
            success=True,
            message=f"RestartService: service={self.service}",
            timestamp_ns=timebase.now_ns(),
        )
