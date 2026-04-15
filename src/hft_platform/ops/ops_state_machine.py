"""OperationsStateMachine — daily lifecycle orchestrator above SessionGovernor."""
from __future__ import annotations

import enum
from typing import Any, Awaitable, Callable

import structlog

from hft_platform.core import timebase
from hft_platform.notifications.alert import Alert, AlertSeverity
from hft_platform.ops.preflight_checker import PreflightChecker, PreflightReport

logger = structlog.get_logger("ops.ops_state_machine")


def _make_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8]


class OpsState(enum.StrEnum):
    MAINTENANCE = "maintenance"
    PRE_MARKET = "pre_market"
    TRADING = "trading"
    POST_MARKET = "post_market"
    SETTLEMENT = "settlement"
    NIGHT_SESSION = "night_session"


class OperationsStateMachine:
    """Orchestrates daily lifecycle above SessionGovernor."""

    __slots__ = (
        "_state", "_session_governor", "_preflight_checker",
        "_alert_callback", "_state_history", "_callbacks",
    )

    def __init__(
        self,
        session_governor: Any,
        preflight_checker: PreflightChecker,
        alert_callback: Callable[[Alert], Awaitable[None]],
    ) -> None:
        self._state = OpsState.MAINTENANCE
        self._session_governor = session_governor
        self._preflight_checker = preflight_checker
        self._alert_callback = alert_callback
        self._state_history: list[tuple[int, OpsState]] = []
        self._callbacks: list[Callable[[OpsState, OpsState], Awaitable[None]]] = []

    @property
    def state(self) -> OpsState:
        return self._state

    @property
    def state_history(self) -> list[tuple[int, OpsState]]:
        return list(self._state_history)

    def register_callback(self, callback: Callable[[OpsState, OpsState], Awaitable[None]]) -> None:
        self._callbacks.append(callback)

    async def transition_to(self, new_state: OpsState) -> None:
        if new_state == self._state:
            return
        old_state = self._state
        self._state = new_state
        now_ns = timebase.now_ns()
        self._state_history.append((now_ns, new_state))
        logger.info("ops_state_transition", old=old_state.value, new=new_state.value)
        alert = Alert(
            alert_id=_make_id(),
            severity=AlertSeverity.INFO,
            category="ops",
            source="ops_state_machine",
            title=f"Ops: {old_state.value} -> {new_state.value}",
            detail=f"Operations state changed from {old_state.value} to {new_state.value}",
            ts_ns=now_ns,
            dedup_key=None,
            metadata={"old_state": old_state.value, "new_state": new_state.value},
        )
        await self._alert_callback(alert)
        for cb in self._callbacks:
            try:
                await cb(old_state, new_state)
            except Exception as exc:
                logger.error("ops_state_callback_error", error=str(exc))

    async def run_preflight(self) -> PreflightReport:
        return await self._preflight_checker.run_all()
