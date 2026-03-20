"""Incident management — alert-to-action auto-mitigation framework."""

from __future__ import annotations

from hft_platform.incident.actions import (
    ActionResult,
    CancelOpenOrdersAction,
    LogAndEscalateAction,
    MitigationAction,
    NoOpAction,
    ReconnectBrokerAction,
    RestartServiceAction,
    SwitchRecorderModeAction,
)
from hft_platform.incident.auto_mitigation import AlertMitigator

__all__ = [
    "ActionResult",
    "AlertMitigator",
    "CancelOpenOrdersAction",
    "LogAndEscalateAction",
    "MitigationAction",
    "NoOpAction",
    "ReconnectBrokerAction",
    "RestartServiceAction",
    "SwitchRecorderModeAction",
]
