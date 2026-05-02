"""CE2-06: GatewayPolicy FSM — NORMAL / DEGRADE / HALT mode gating.

Rules:
- HALT:    blocks NEW/AMEND intents; allows CANCEL, FORCE_FLAT, and halt-exempt strategies.
- DEGRADE: blocks NEW and AMEND; allows CANCEL, FORCE_FLAT, and halt-exempt strategies
           (triggered automatically on StormGuard STORM).
- NORMAL:  allows all intents.

Mode is readable as a Prometheus gauge (gateway_policy_mode).
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Any, Callable

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState

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

_SAFETY_INTENT_TYPES: frozenset[int] = frozenset({int(IntentType.CANCEL), int(IntentType.FORCE_FLAT)})


class GatewayPolicy:
    """Stateful FSM gating intents based on system risk state.

    Env vars:
        HFT_GATEWAY_HALT_CANCEL:         allow CANCEL in HALT (default 1)
        HFT_GATEWAY_DEGRADE_ON_STORM:    auto-degrade on StormGuard STORM (default 1)
        HFT_GATEWAY_STARTUP_HOLDOFF_S:   block NEW/AMEND for N seconds after init (default 60)
    """

    def __init__(
        self,
        storm_guard: Any | None = None,
        position_provider: Callable[[str, str], int] | None = None,
    ) -> None:
        self._mode = GatewayPolicyMode.NORMAL
        self._halt_cancel = os.getenv("HFT_GATEWAY_HALT_CANCEL", "1").lower() not in {"0", "false", "no", "off"}
        self._degrade_on_storm = os.getenv("HFT_GATEWAY_DEGRADE_ON_STORM", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._storm_guard = storm_guard
        # Bug 22 (2026-04-17): allow reducing orders through HALT/DEGRADE even
        # if strategy is not halt-exempt. Without this, R47 stuck short cannot
        # cover through Gateway when StormGuard is in HALT (Bug 21 fixed the
        # validator/StormGuard layers but Gateway runs first in the pipeline).
        self._position_provider: Callable[[str, str], int] | None = position_provider
        holdoff_s = float(os.getenv("HFT_GATEWAY_STARTUP_HOLDOFF_S", "60"))
        self._startup_holdoff_until: float = time.monotonic() + holdoff_s if holdoff_s > 0 else 0.0

    # ── Public ────────────────────────────────────────────────────────────

    def gate(
        self,
        intent: OrderIntent,
        sg_state: StormGuardState,
    ) -> tuple[bool, str]:
        return self._gate_by_intent_type(
            int(intent.intent_type),
            sg_state,
            strategy_id=intent.strategy_id,
            symbol=intent.symbol,
            side=int(intent.side),
            qty=int(intent.qty),
        )

    def gate_typed(
        self,
        intent_type: int,
        sg_state: StormGuardState,
        strategy_id: str = "",
        symbol: str = "",
        side: int = 0,
        qty: int = 0,
    ) -> tuple[bool, str]:
        """Typed fast-path gate using raw intent_type int to avoid enum conversion overhead.

        Bug 22: optional ``symbol``/``side``/``qty`` enable reduce-only bypass in
        HALT/DEGRADE. Callers that don't supply them fall back to legacy behaviour
        (block opens, allow CANCEL/FORCE_FLAT only).
        """
        return self._gate_by_intent_type(
            int(intent_type),
            sg_state,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            qty=qty,
        )

    def _gate_by_intent_type(
        self,
        intent_type: int,
        sg_state: StormGuardState,
        strategy_id: str = "",
        symbol: str = "",
        side: int = 0,
        qty: int = 0,
    ) -> tuple[bool, str]:
        """Evaluate current mode + StormGuard state; return (allowed, reason).

        Side-effect: auto-transitions to DEGRADE on STORM if configured.
        """
        # Startup holdoff: block NEW/AMEND during initial STORM→NORMAL transient
        if self._startup_holdoff_until > 0 and intent_type not in _SAFETY_INTENT_TYPES:
            if time.monotonic() < self._startup_holdoff_until:
                return False, "STARTUP_HOLDOFF"

        # Auto-degrade on storm
        if self._degrade_on_storm and sg_state >= StormGuardState.STORM and self._mode == GatewayPolicyMode.NORMAL:
            self._set_mode(GatewayPolicyMode.DEGRADE)

        # Recovery: back to NORMAL when storm clears
        if sg_state < StormGuardState.STORM and self._mode == GatewayPolicyMode.DEGRADE:
            self._set_mode(GatewayPolicyMode.NORMAL)

        if self._mode == GatewayPolicyMode.HALT:
            if intent_type == int(IntentType.FORCE_FLAT):
                return True, "OK"
            if intent_type == int(IntentType.CANCEL) and self._halt_cancel:
                return True, "OK"
            # Bug 22: reducing orders (cover / partial exit) always allowed in HALT
            if self._reduces_position(symbol, strategy_id, side, qty):
                return True, "HALT_REDUCE_ONLY"
            if strategy_id and self._is_halt_exempt(strategy_id):
                return True, "HALT_EXEMPT"
            return False, "HALT"

        if self._mode == GatewayPolicyMode.DEGRADE:
            # In DEGRADE, only allow risk-reducing operations (CANCEL, FORCE_FLAT)
            # and halt-exempt strategies. Bug 22: also allow position-reducing orders.
            if intent_type not in _SAFETY_INTENT_TYPES:
                if self._reduces_position(symbol, strategy_id, side, qty):
                    return True, "DEGRADE_REDUCE_ONLY"
                if strategy_id and self._is_halt_exempt(strategy_id):
                    return True, "DEGRADE_EXEMPT"
                return False, "DEGRADE"

        return True, "OK"

    def _reduces_position(self, symbol: str, strategy_id: str, side: int, qty: int) -> bool:
        """Bug 22 helper: True iff the order strictly reduces ``abs(net_position)``.

        Conservative: missing ``position_provider``, missing symbol, or any
        exception returns False → falls back to the legacy (block) path.
        """
        provider = self._position_provider
        if provider is None or not symbol or qty <= 0:
            return False
        try:
            current = int(provider(symbol, strategy_id) or 0)
        except Exception:  # noqa: BLE001 — never raise from a predicate
            return False
        if current == 0:
            return False
        signed = int(qty) if side == int(Side.BUY) else -int(qty)
        return abs(current + signed) < abs(current)

    def set_position_provider(self, provider: Callable[[str, str], int] | None) -> None:
        """Bug 22: wire position provider post-construction.

        Bootstrap may build GatewayPolicy before RiskEngine; this setter closes
        the loop so reduce-only orders can bypass HALT/DEGRADE.
        """
        self._position_provider = provider

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

    def _is_halt_exempt(self, strategy_id: str) -> bool:
        """Check if a strategy is halt-exempt via StormGuard."""
        sg = self._storm_guard
        if sg is None:
            return False
        is_exempt = getattr(sg, "is_halt_exempt", None)
        if callable(is_exempt):
            return is_exempt(strategy_id)
        return strategy_id in getattr(sg, "_halt_exempt_strategies", frozenset())

    def _set_mode(self, new_mode: GatewayPolicyMode) -> None:
        if new_mode == self._mode:
            return
        logger.warning("GatewayPolicy transition", old=self._mode.value, new=new_mode.value)
        self._mode = new_mode
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            MetricsRegistry.get().gateway_policy_mode.set(self.mode_int())
        except Exception as exc:
            logger.warning("policy_metrics_failed", error=str(exc))
