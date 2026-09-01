from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, Side
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.autonomy import AutonomyMode, AutonomyTransition
from hft_platform.ops.evidence import get_shared_autonomy_evidence_writer

logger = get_logger("strategy_governor")

_AUTONOMY_MODE_VALUES = {
    AutonomyMode.NORMAL: 0,
    AutonomyMode.STRATEGY_QUARANTINED: 1,
    AutonomyMode.PLATFORM_REDUCE_ONLY: 2,
    AutonomyMode.HALT: 3,
}


#: Identifies this engine run. A quarantine token carries it so a re-arm request
#: written against a *previous* run can never match a live quarantine after a
#: restart, when the per-process counter starts over at 1. Full uuid4, not a
#: truncation: a PID and a sequence number both repeat across restarts, so the
#: run id is the only part carrying the non-collision guarantee.
_RUN_ID = f"{os.getpid():d}-{uuid.uuid4().hex}"


@dataclass(slots=True, frozen=True)
class StrategyQuarantine:
    strategy_id: str
    reason: str
    transition: AutonomyTransition
    #: Unique per quarantine *instance*, not per strategy. An operator re-arm
    #: must name the exact token it intends to clear; see ``rearm``.
    token: str = ""


class StrategyHealthGovernor:
    def __init__(self, metrics=None, evidence_writer=None):
        self.metrics = metrics or MetricsRegistry.get()
        self.evidence_writer = evidence_writer or get_shared_autonomy_evidence_writer()
        self._quarantined: dict[str, StrategyQuarantine] = {}
        self._quarantine_seq: int = 0

    def quarantine_token(self, strategy_id: str) -> str | None:
        """Token of the strategy's live quarantine, or ``None`` if not quarantined."""
        entry = self._quarantined.get(strategy_id)
        return entry.token if entry is not None else None

    def quarantine(self, strategy_id: str, *, reason: str) -> AutonomyTransition:
        from_mode = AutonomyMode.STRATEGY_QUARANTINED if strategy_id in self._quarantined else AutonomyMode.NORMAL
        transition = self._build_transition(from_mode=from_mode, reason=reason)
        self._quarantine_seq += 1
        token = f"{_RUN_ID}:{strategy_id}:{self._quarantine_seq}"
        self._quarantined[strategy_id] = StrategyQuarantine(
            strategy_id=strategy_id,
            reason=reason,
            transition=transition,
            token=token,
        )
        self._set_strategy_quarantine_active(strategy_id, active=True)
        self._set_strategy_scope_state()
        transition.record_transition(self.metrics)
        if self.evidence_writer is not None:
            self.evidence_writer.record_transition(
                scope="strategy",
                mode=transition.to_mode.value,
                reason=transition.reason,
                manual_rearm_required=transition.manual_rearm_required,
                metadata={"strategy_id": strategy_id, "quarantine_token": token},
            )
        logger.warning("strategy_quarantined", strategy_id=strategy_id, reason=reason, quarantine_token=token)
        return transition

    def is_quarantined(self, strategy_id: str) -> bool:
        return strategy_id in self._quarantined

    def rearm(self, strategy_id: str, *, expected_token: str, request_id: str | None = None) -> bool:
        """Clear one strategy's quarantine, and only the exact one authorized.

        ``expected_token`` names a specific quarantine *instance*, so an
        authorization issued for an earlier failure can never release a later
        one. The comparison and the removal happen together, with no await and
        no IO between them, so nothing can interleave: on the event loop this is
        atomic by construction.

        The evidence write follows the decision rather than gating it. That
        ordering is safe here for a reason specific to this state -- **a
        strategy quarantine does not survive a restart**; only the platform
        scope has a boot-time restore. So the record is an audit trail, not a
        durability guarantee, and a filesystem failure must not be allowed to
        block a recovery the operator has already authorized. It is recorded
        best-effort and loudly on failure.

        Returns ``True`` only when a live quarantine was actually cleared.
        """
        entry = self._quarantined.get(strategy_id)
        if entry is None or entry.token != expected_token:
            return False

        # Decide and mutate with nothing in between.
        self._quarantined.pop(strategy_id, None)
        self._set_strategy_quarantine_active(strategy_id, active=False)
        self._set_strategy_scope_state()
        logger.info(
            "strategy_rearmed",
            strategy_id=strategy_id,
            quarantine_token=entry.token,
            request_id=request_id,
        )

        if self.evidence_writer is not None:
            try:
                self.evidence_writer.record_transition(
                    scope="strategy",
                    mode=AutonomyMode.NORMAL.value,
                    reason="manual_rearm",
                    manual_rearm_required=False,
                    metadata={
                        "strategy_id": strategy_id,
                        "quarantine_token": entry.token,
                        "request_id": request_id,
                    },
                )
            except Exception as exc:
                # The strategy is re-armed either way; losing the audit record
                # must not be silent.
                logger.error(
                    "strategy_rearm_evidence_write_failed",
                    strategy_id=strategy_id,
                    request_id=request_id,
                    error=str(exc),
                )
        return True

    def build_cancel_intents(
        self,
        strategy_id: str,
        *,
        live_orders: Iterable[tuple[str, str]],
        intent_factory,
        source_ts_ns: int | None = None,
        trace_id: str | None = None,
    ) -> list[Any]:
        quarantine = self._quarantined.get(strategy_id)
        if quarantine is None:
            return []

        tagged_reason = f"strategy_quarantined:{quarantine.transition.reason}"
        intents = []
        for symbol, order_id in live_orders:
            intent = intent_factory(
                strategy_id=strategy_id,
                symbol=symbol,
                side=Side.BUY,
                price=0,
                qty=0,
                tif=TIF.LIMIT,
                intent_type=IntentType.CANCEL,
                target_order_id=order_id,
                source_ts_ns=source_ts_ns,
                trace_id=trace_id,
            )
            intent = self._tag_intent_reason(intent, tagged_reason)
            intents.append(intent)
        return intents

    def _set_strategy_quarantine_active(self, strategy_id: str, *, active: bool) -> None:
        if not self.metrics:
            return
        metric = getattr(self.metrics, "strategy_quarantine_active", None)
        if metric is None:
            return
        metric.labels(strategy=strategy_id).set(1 if active else 0)

    def _set_strategy_scope_state(self) -> None:
        if not self.metrics:
            return
        mode = AutonomyMode.STRATEGY_QUARANTINED if self._quarantined else AutonomyMode.NORMAL

        autonomy_mode = getattr(self.metrics, "autonomy_mode", None)
        if autonomy_mode is not None:
            autonomy_mode.labels(scope="strategy").set(_AUTONOMY_MODE_VALUES[mode])

        manual_rearm_required = getattr(self.metrics, "manual_rearm_required", None)
        if manual_rearm_required is not None:
            manual_rearm_required.labels(scope="strategy").set(1 if self._quarantined else 0)

    def _build_transition(self, *, from_mode: AutonomyMode, reason: str) -> AutonomyTransition:
        transition = AutonomyTransition(
            scope="strategy",
            from_mode=from_mode,
            to_mode=AutonomyMode.STRATEGY_QUARANTINED,
            reason=reason,
            manual_rearm_required=True,
        )
        if transition.metric_reason != "unknown":
            return transition

        strategy_reason = f"strategy_{reason}"
        mapped_transition = AutonomyTransition(
            scope="strategy",
            from_mode=from_mode,
            to_mode=AutonomyMode.STRATEGY_QUARANTINED,
            reason=strategy_reason,
            manual_rearm_required=True,
        )
        if mapped_transition.metric_reason != "unknown":
            return mapped_transition
        return transition

    def _tag_intent_reason(self, intent: Any, reason: str) -> Any:
        if isinstance(intent, tuple) and len(intent) >= 16 and intent[0] == "typed_intent_v1":
            tagged_intent = list(intent)
            tagged_intent[12] = reason
            return tuple(tagged_intent)
        if isinstance(intent, dict):
            intent["reason"] = reason
            return intent
        if hasattr(intent, "reason"):
            intent.reason = reason
        return intent
