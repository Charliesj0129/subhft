"""Alert auto-mitigation — maps alert fingerprints to conservative actions.

All registered actions are conservative: reconnect, mode-switch, cancel, log.
Never auto-places orders.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.incident.actions import (
    BaseAction,
    CancelOpenOrdersAction,
    LogAndEscalateAction,
    MitigationAction,
    NoOpAction,
    ReconnectBrokerAction,
    RestartServiceAction,
    SwitchRecorderModeAction,
)

logger = get_logger("incident.auto_mitigation")


def _build_default_registry() -> dict[str, BaseAction]:
    """Build the default alert-to-action registry."""
    return {
        "FeedGapCritical": ReconnectBrokerAction(max_retries=3, backoff_base_s=2.0),
        "RecorderFailure": SwitchRecorderModeAction(target_mode="wal_first"),
        "BusOverflowCritical": LogAndEscalateAction(severity="critical"),
        "StormGuardHalt": CancelOpenOrdersAction(),
        "ExecutionGatewayTaskDown": RestartServiceAction(service="exec_gateway"),
    }


class AlertMitigator:
    """Evaluate fired alerts and select appropriate mitigation actions.

    Thread-safe: the action registry is immutable after construction.
    """

    __slots__ = ("_registry",)

    def __init__(self, registry: dict[str, BaseAction] | None = None) -> None:
        self._registry: dict[str, BaseAction] = registry or _build_default_registry()

    def evaluate(
        self,
        alert_name: str,
        labels: dict[str, str] | None = None,
        annotations: dict[str, str] | None = None,
    ) -> MitigationAction:
        """Evaluate an alert and return the appropriate mitigation action.

        Args:
            alert_name: Name of the fired alert (e.g. 'FeedGapCritical').
            labels: Prometheus alert labels.
            annotations: Prometheus alert annotations.

        Returns:
            MitigationAction describing what to do.
        """
        labels = labels or {}
        annotations = annotations or {}

        action = self._registry.get(alert_name)
        if action is None:
            noop = NoOpAction(reason=f"no handler for alert '{alert_name}'")
            logger.info(
                "No mitigation handler",
                alert_name=alert_name,
                labels=labels,
            )
            return MitigationAction(
                action_type="noop",
                params={"alert_name": alert_name},
                safe=True,
                reason=noop.reason,
                timestamp_ns=timebase.now_ns(),
            )

        safe = action.is_safe()
        params: dict[str, Any] = {"alert_name": alert_name, "labels": labels}
        for attr in ("max_retries", "backoff_base_s", "target_mode", "severity", "service"):
            if hasattr(action, attr):
                params[attr] = getattr(action, attr)

        logger.info(
            "Mitigation selected",
            alert_name=alert_name,
            action_type=action.action_type,
            safe=safe,
            labels=labels,
        )

        return MitigationAction(
            action_type=action.action_type,
            params=params,
            safe=safe,
            reason=f"auto-mitigation for {alert_name}",
            timestamp_ns=timebase.now_ns(),
        )

    def execute(
        self,
        alert_name: str,
        labels: dict[str, str] | None = None,
        annotations: dict[str, str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[MitigationAction, Any]:
        """Evaluate and execute an action for the given alert.

        Returns:
            Tuple of (MitigationAction, ActionResult).
        """
        mitigation = self.evaluate(alert_name, labels, annotations)
        action = self._registry.get(alert_name, NoOpAction(reason=f"no handler for '{alert_name}'"))

        if not action.is_safe():
            logger.warning(
                "Action safety gate blocked execution",
                alert_name=alert_name,
                action_type=action.action_type,
            )
            return mitigation, None

        result = action.execute(context)
        logger.info(
            "Mitigation executed",
            alert_name=alert_name,
            action_type=action.action_type,
            success=result.success,
            message=result.message,
        )
        return mitigation, result
