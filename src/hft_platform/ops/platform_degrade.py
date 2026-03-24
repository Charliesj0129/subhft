from __future__ import annotations

from threading import Lock
from typing import Any

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType
from hft_platform.ops.autonomy import AutonomyMode, AutonomyTransition
from hft_platform.ops.evidence import get_shared_autonomy_evidence_writer

_AUTONOMY_MODE_VALUES = {
    AutonomyMode.NORMAL: 0,
    AutonomyMode.STRATEGY_QUARANTINED: 1,
    AutonomyMode.PLATFORM_REDUCE_ONLY: 2,
    AutonomyMode.HALT: 3,
}

_shared_controller: "PlatformDegradeController | None" = None
_shared_controller_lock = Lock()
logger = get_logger("platform_degrade")


class PlatformDegradeController:
    def __init__(self, metrics: Any | None = None, evidence_writer: Any | None = None) -> None:
        self.metrics = metrics or self._default_metrics()
        self.evidence_writer = evidence_writer or get_shared_autonomy_evidence_writer()
        self.reduce_only_active = False
        self.last_transition: AutonomyTransition | None = None
        self._reference_positions: dict[str, int] = {}
        self._reference_close_reservations: dict[str, int] = {}
        self._sync_metrics()

    @staticmethod
    def _default_metrics() -> Any | None:
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            return MetricsRegistry.get()
        except Exception:
            return None

    def enter_reduce_only(self, *, reason: str) -> AutonomyTransition:
        if self.reduce_only_active and self.last_transition is not None:
            return self.last_transition

        transition = AutonomyTransition.enter_platform_reduce_only(
            reason,
            from_mode=AutonomyMode.NORMAL if not self.reduce_only_active else AutonomyMode.PLATFORM_REDUCE_ONLY,
        )
        self.reduce_only_active = True
        self.last_transition = transition
        self._sync_metrics()
        logger.warning(
            "platform_reduce_only_entered",
            reason=reason,
            from_mode=transition.from_mode.value,
            to_mode=transition.to_mode.value,
            manual_rearm_required=transition.manual_rearm_required,
        )
        if self.evidence_writer is not None:
            self.evidence_writer.record_transition(
                scope="platform",
                mode=transition.to_mode.value,
                reason=transition.reason,
                manual_rearm_required=transition.manual_rearm_required,
            )
        if self.metrics is not None:
            transition.record_transition(self.metrics)
        return transition

    def allow_open(self) -> bool:
        return not self.reduce_only_active

    def allow_close(self) -> bool:
        return True

    def allow_intent(self, *, intent_type: IntentType | int | str, opens_risk: bool) -> bool:
        normalized_intent = self._normalize_intent_type(intent_type)
        if not self.reduce_only_active:
            return True
        if normalized_intent in {IntentType.CANCEL, IntentType.AMEND}:
            return True
        if normalized_intent == IntentType.NEW:
            return not opens_risk
        return True

    def update_reference_positions(self, *, local_map: dict[str, int], broker_map: dict[str, int]) -> None:
        reference_positions: dict[str, int] = {}
        for symbol in set(local_map) | set(broker_map):
            broker_qty = int(broker_map.get(symbol, 0))
            local_qty = int(local_map.get(symbol, 0))
            reference_positions[symbol] = broker_qty if broker_qty != 0 else local_qty
        self._reference_positions = reference_positions
        self._reference_close_reservations = {}

    def reference_net_qty(self, symbol: str) -> int | None:
        if symbol not in self._reference_positions:
            return None
        return self._reference_positions[symbol]

    def reference_available_net_qty(self, symbol: str) -> int | None:
        reference_qty = self.reference_net_qty(symbol)
        if reference_qty is None:
            return None
        reserved_qty = int(self._reference_close_reservations.get(symbol, 0))
        if reference_qty > 0:
            return max(0, reference_qty - reserved_qty)
        if reference_qty < 0:
            return min(0, reference_qty + reserved_qty)
        return 0

    def reserve_reference_close(self, *, symbol: str, qty: int) -> None:
        if qty <= 0 or symbol not in self._reference_positions:
            return
        self._reference_close_reservations[symbol] = self._reference_close_reservations.get(symbol, 0) + int(qty)

    @staticmethod
    def _normalize_intent_type(intent_type: IntentType | int | str) -> IntentType | None:
        if isinstance(intent_type, IntentType):
            return intent_type
        try:
            if isinstance(intent_type, str):
                return IntentType[intent_type]
            return IntentType(intent_type)
        except Exception:
            return None

    def _sync_metrics(self) -> None:
        if self.metrics is None:
            return
        autonomy_mode = getattr(self.metrics, "autonomy_mode", None)
        if autonomy_mode is not None:
            mode = AutonomyMode.PLATFORM_REDUCE_ONLY if self.reduce_only_active else AutonomyMode.NORMAL
            autonomy_mode.labels(scope="platform").set(_AUTONOMY_MODE_VALUES[mode])
        platform_reduce_only_active = getattr(self.metrics, "platform_reduce_only_active", None)
        if platform_reduce_only_active is not None:
            platform_reduce_only_active.set(1 if self.reduce_only_active else 0)
        manual_rearm_required = getattr(self.metrics, "manual_rearm_required", None)
        if manual_rearm_required is not None:
            manual_rearm_required.labels(scope="platform").set(1 if self.reduce_only_active else 0)


def get_shared_platform_degrade_controller(*, metrics: Any | None = None) -> PlatformDegradeController:
    global _shared_controller
    with _shared_controller_lock:
        if _shared_controller is None:
            _shared_controller = PlatformDegradeController(metrics=metrics)
        elif metrics is not None and _shared_controller.metrics is None:
            _shared_controller.metrics = metrics
            _shared_controller._sync_metrics()
        return _shared_controller


def reset_shared_platform_degrade_controller() -> None:
    global _shared_controller
    with _shared_controller_lock:
        _shared_controller = None
