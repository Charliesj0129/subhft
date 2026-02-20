"""CE2-06: GatewayPolicy FSM — NORMAL / DEGRADE / HALT mode gating.

Rules:
- HALT:    blocks NEW intents; allows CANCEL if HFT_GATEWAY_HALT_CANCEL=1.
- DEGRADE: blocks NEW intents only (triggered automatically on StormGuard STORM).
- NORMAL:  allows all intents.

Mode is readable as a Prometheus gauge (gateway_policy_mode).
"""
from __future__ import annotations

import os
from enum import Enum

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, StormGuardState

logger = get_logger("gateway.policy")


class GatewayPolicyMode(str, Enum):
    NORMAL = "NORMAL"
    DEGRADE = "DEGRADE"
    HALT = "HALT"


_MODE_TO_INT = {
    GatewayPolicyMode.NORMAL: 0,
    GatewayPolicyMode.DEGRADE: 1,
    GatewayPolicyMode.HALT: 2,
}


class GatewayPolicy:
    """Stateful FSM gating intents based on system risk state.

    Env vars:
        HFT_GATEWAY_HALT_CANCEL:      allow CANCEL in HALT (default 1)
        HFT_GATEWAY_DEGRADE_ON_STORM: auto-degrade on StormGuard STORM (default 1)
    """

    def __init__(self) -> None:
        self._mode = GatewayPolicyMode.NORMAL
        self._halt_cancel = os.getenv("HFT_GATEWAY_HALT_CANCEL", "1").lower() not in {"0", "false", "no", "off"}
        self._degrade_on_storm = os.getenv("HFT_GATEWAY_DEGRADE_ON_STORM", "1").lower() not in {
            "0", "false", "no", "off"
        }

    # ── Public ────────────────────────────────────────────────────────────

    def gate(
        self,
        intent: OrderIntent,
        sg_state: StormGuardState,
    ) -> tuple[bool, str]:
        """Evaluate current mode + StormGuard state; return (allowed, reason).

        Side-effect: auto-transitions to DEGRADE on STORM if configured.
        """
        # Auto-degrade on storm
        if self._degrade_on_storm and sg_state >= StormGuardState.STORM and self._mode == GatewayPolicyMode.NORMAL:
            self._set_mode(GatewayPolicyMode.DEGRADE)

        # Recovery: back to NORMAL when storm clears
        if sg_state < StormGuardState.STORM and self._mode == GatewayPolicyMode.DEGRADE:
            self._set_mode(GatewayPolicyMode.NORMAL)

        if self._mode == GatewayPolicyMode.HALT:
            if intent.intent_type == IntentType.CANCEL and self._halt_cancel:
                return True, "OK"
            return False, "HALT"

        if self._mode == GatewayPolicyMode.DEGRADE:
            if intent.intent_type == IntentType.NEW:
                return False, "DEGRADE"

        return True, "OK"

    def set_halt(self) -> None:
        self._set_mode(GatewayPolicyMode.HALT)

    def set_normal(self) -> None:
        self._set_mode(GatewayPolicyMode.NORMAL)

    @property
    def mode(self) -> GatewayPolicyMode:
        return self._mode

    def mode_int(self) -> int:
        return _MODE_TO_INT[self._mode]

    # ── Private ───────────────────────────────────────────────────────────

    def _set_mode(self, new_mode: GatewayPolicyMode) -> None:
        if new_mode == self._mode:
            return
        logger.warning("GatewayPolicy transition", old=self._mode.value, new=new_mode.value)
        self._mode = new_mode
        try:
            from hft_platform.observability.metrics import MetricsRegistry
            MetricsRegistry.get().gateway_policy_mode.set(self.mode_int())
        except Exception:
            pass
