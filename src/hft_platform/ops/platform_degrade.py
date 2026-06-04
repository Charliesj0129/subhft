from __future__ import annotations

import os
import time
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

_AUTO_RECOVERABLE_REASONS: frozenset[str] = frozenset(
    {
        "feed_reconnect_unhealthy",
        "feed_reconnect_pending",
        "feed_gap_exceeded",
        "feed_reconnect_flapping",
        "reconciliation_drift",
        "rss_unhealthy",
        # The reasons below are all level-based in platform_inputs.reduce_only_reasons:
        # re-emitted each tick only while the live probe still observes the failure.
        # Without auto-recovery they latch indefinitely after a transient blip and
        # the docker-exec rearm CLI cannot reach _shared_controller to clear them.
        # `recorder_data_loss` is intentionally excluded: autonomy_monitor routes it
        # to trigger_halt (data-integrity gate, manual reconciliation required).
        "queue_depth_exceeded",
        "redis_unhealthy",
        "wal_backlog_unhealthy",
        "clickhouse_unhealthy",
    }
)

_shared_controller: "PlatformDegradeController | None" = None
_shared_controller_lock = Lock()
logger = get_logger("platform_degrade")


class PlatformDegradeController:
    def __init__(
        self,
        *,
        metrics: Any | None = None,
        evidence_writer: Any | None = None,
        shadow_mode: bool = False,
        auto_recovery_enabled: bool = True,
        auto_recovery_cooldown_s: int = 60,
    ) -> None:
        self._shadow_mode = shadow_mode
        self._auto_recovery_enabled = auto_recovery_enabled
        self._auto_recovery_cooldown_s = auto_recovery_cooldown_s
        self._auto_recovery_cooldown_ns = int(auto_recovery_cooldown_s * 1_000_000_000)
        self._recovery_started_ns: int = 0
        self.metrics = metrics or self._default_metrics()
        self.evidence_writer = evidence_writer or get_shared_autonomy_evidence_writer()
        self.reduce_only_active = False
        self.last_transition: AutonomyTransition | None = None
        self._reference_positions: dict[str, int] = {}
        self._reference_close_reservations: dict[str, int] = {}
        self._active_reasons: set[str] = set()
        # Rate-limit repeated "reason_added" log (break RSS→log feedback loop)
        self._reason_last_log_ns: dict[str, int] = {}
        self._reason_log_interval_ns: int = int(
            float(os.getenv("HFT_REDUCE_ONLY_LOG_INTERVAL_S", "60")) * 1_000_000_000
        )
        self._sync_metrics()

    @staticmethod
    def _default_metrics() -> Any | None:
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            return MetricsRegistry.get()
        except Exception:
            return None

    @property
    def manual_rearm_required_active(self) -> bool:
        """True only when a genuine, non-auto-recoverable reason is active.

        Auto-recoverable reasons (feed reconnect, queue depth, etc.) self-clear
        via :meth:`check_auto_recovery`; they must NOT be reported as requiring
        operator intervention. Tying the manual-rearm signal to ``reduce_only_active``
        alone is what let a transient ``feed_reconnect_pending`` masquerade as a
        permanent manual-rearm latch.
        """
        return bool(self._active_reasons - _AUTO_RECOVERABLE_REASONS)

    def enter_reduce_only(self, *, reason: str) -> AutonomyTransition:
        self._active_reasons.add(reason)
        if self.reduce_only_active and self.last_transition is not None:
            # Rate-limit repeated log to break RSS→log→heap feedback loop
            now_ns = time.monotonic_ns()
            last_ns = self._reason_last_log_ns.get(reason, 0)
            if now_ns - last_ns >= self._reason_log_interval_ns:
                self._reason_last_log_ns[reason] = now_ns
                logger.info(
                    "platform_reduce_only_reason_active",
                    reason=reason,
                    active_reasons=sorted(self._active_reasons),
                )
            return self.last_transition

        transition = AutonomyTransition.enter_platform_reduce_only(
            reason,
            from_mode=AutonomyMode.NORMAL if not self.reduce_only_active else AutonomyMode.PLATFORM_REDUCE_ONLY,
            # Auto-recoverable reasons self-clear; persisting a manual-rearm flag
            # for them is what created the self-perpetuating phantom latch.
            manual_rearm_required=reason not in _AUTO_RECOVERABLE_REASONS,
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
            active_reasons=sorted(self._active_reasons),
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

    def exit_reduce_only(self, *, reason: str) -> AutonomyTransition:
        if not self.reduce_only_active:
            return AutonomyTransition.enter_platform_reduce_only(
                reason,
                from_mode=AutonomyMode.NORMAL,
            )

        transition = AutonomyTransition.exit_platform_reduce_only(
            reason,
            from_mode=AutonomyMode.PLATFORM_REDUCE_ONLY,
        )
        self.reduce_only_active = False
        self.last_transition = transition
        self._reference_positions = {}
        self._reference_close_reservations = {}
        self._active_reasons.clear()
        self._reason_last_log_ns.clear()
        self._sync_metrics()
        logger.info(
            "platform_reduce_only_exited",
            reason=reason,
            from_mode=transition.from_mode.value,
            to_mode=transition.to_mode.value,
        )
        if self.evidence_writer is not None:
            self.evidence_writer.record_transition(
                scope="platform",
                mode=transition.to_mode.value,
                reason=transition.reason,
                manual_rearm_required=False,
            )
        if self.metrics is not None:
            transition.record_transition(self.metrics)
        return transition

    def force_clear(self, *, reason: str = "manual_rearm") -> AutonomyTransition | None:
        """Operator escape hatch: clear all reasons and exit reduce_only.

        Used by :meth:`hft_platform.ops.manual_rearm.ManualRearmGate.rearm_platform`
        to bridge the previously broken interface — until this method
        existed, manual rearm wrote a JSON flag that the controller
        never consulted, so reduce_only could remain latched
        indefinitely even after the operator confirmed.

        This bypasses the auto-recovery cooldown and the
        "all reasons must be empty" gate: the operator has explicitly
        attested that conditions are safe.  Use sparingly; the auto
        path is preferred when reasons clear naturally.

        Returns the resulting :class:`AutonomyTransition`, or ``None``
        if reduce_only was already inactive.
        """
        if not self.reduce_only_active:
            return None
        # Drop reasons before exit so observability reflects the manual
        # override path rather than a phantom remaining condition.
        cleared_reasons = sorted(self._active_reasons)
        self._active_reasons.clear()
        self._reason_last_log_ns.clear()
        self._recovery_started_ns = 0
        logger.warning(
            "platform_reduce_only_force_cleared",
            reason=reason,
            cleared_reasons=cleared_reasons,
        )
        return self.exit_reduce_only(reason=f"manual_force_clear:{reason}")

    def check_auto_recovery(self, *, current_reasons: list[str], now_ns: int) -> bool:
        """Check if auto-recovery should trigger. Called from supervisor loop.

        Returns True if recovery was performed.
        """
        if not self.reduce_only_active or not self._auto_recovery_enabled:
            return False

        # Sync: remove auto-recoverable reasons that inputs no longer report
        input_reason_set = set(current_reasons)
        auto_recoverable_active = self._active_reasons & _AUTO_RECOVERABLE_REASONS
        cleared = auto_recoverable_active - input_reason_set
        if cleared:
            self._active_reasons -= cleared
            logger.info(
                "auto_recovery_reasons_cleared", cleared=sorted(cleared), remaining=sorted(self._active_reasons)
            )

        # Sync: re-add auto-recoverable reasons that are re-firing in inputs
        re_fired = (input_reason_set & _AUTO_RECOVERABLE_REASONS) - self._active_reasons
        if re_fired:
            self._active_reasons |= re_fired
            self._recovery_started_ns = 0
            logger.info(
                "auto_recovery_reasons_refired", refired=sorted(re_fired), active_reasons=sorted(self._active_reasons)
            )

        # If ANY non-auto-recoverable reason remains, block auto-recovery
        non_recoverable = self._active_reasons - _AUTO_RECOVERABLE_REASONS
        if non_recoverable:
            self._recovery_started_ns = 0
            return False

        # If any active reason remains (auto-recoverable but still firing), reset
        if self._active_reasons:
            self._recovery_started_ns = 0
            return False

        # All reasons cleared — run cooldown timer
        if self._recovery_started_ns == 0:
            self._recovery_started_ns = now_ns
            logger.info("auto_recovery_cooldown_started", cooldown_s=self._auto_recovery_cooldown_s)
            return False

        elapsed_ns = now_ns - self._recovery_started_ns
        if elapsed_ns >= self._auto_recovery_cooldown_ns:
            self.exit_reduce_only(reason=f"auto_recovery: all_reasons_cleared_{self._auto_recovery_cooldown_s}s")
            self._recovery_started_ns = 0
            return True

        return False

    def allow_open(self) -> bool:
        return not self.reduce_only_active

    def allow_close(self) -> bool:
        return True

    def allow_intent(self, *, intent_type: IntentType | int | str, opens_risk: bool) -> bool:
        if self._shadow_mode:
            return True
        normalized_intent = self._normalize_intent_type(intent_type)
        if not self.reduce_only_active:
            return True
        if normalized_intent in {IntentType.CANCEL, IntentType.AMEND, IntentType.FORCE_FLAT}:
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
        # Preserve existing close reservations for symbols still present.
        # Only clear reservations for symbols no longer in reference positions.
        old_reservations = self._reference_close_reservations
        self._reference_close_reservations = {
            sym: qty for sym, qty in old_reservations.items() if sym in reference_positions
        }

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
            manual_rearm_required.labels(scope="platform").set(1 if self.manual_rearm_required_active else 0)


def get_shared_platform_degrade_controller(
    *,
    metrics: Any | None = None,
    shadow_mode: bool | None = None,
) -> PlatformDegradeController:
    global _shared_controller
    with _shared_controller_lock:
        if _shared_controller is None:
            _shadow = shadow_mode if shadow_mode is not None else (os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1")
            _auto_enabled = os.getenv("HFT_PLATFORM_AUTO_RECOVERY_ENABLED", "1") == "1"
            try:
                _cooldown = int(os.getenv("HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S", "60"))
            except ValueError:
                _cooldown = 60
            _shared_controller = PlatformDegradeController(
                metrics=metrics,
                shadow_mode=_shadow,
                auto_recovery_enabled=_auto_enabled,
                auto_recovery_cooldown_s=_cooldown,
            )
            _restore_manual_rearm_state(_shared_controller)
        elif metrics is not None and _shared_controller.metrics is None:
            _shared_controller.metrics = metrics
            _shared_controller._sync_metrics()
        return _shared_controller


def _restore_manual_rearm_state(controller: "PlatformDegradeController") -> None:
    """Re-enter reduce_only if a prior run persisted a genuine manual-rearm latch.

    Bridges the docker-exec rearm CLI path: when the operator runs rearm in
    a separate process, the persistence write succeeds but the live engine's
    `_shared_controller` is not reachable. The next engine restart used to
    boot in NORMAL, silently clearing a flag the operator never confirmed.

    Now bootstrap honours the persisted flag — but with two corrections that
    killed the self-perpetuating phantom latch:

    * It restores the ORIGINAL persisted reason rather than the opaque
      ``restored_from_runtime_state`` sentinel, so observability stays honest
      and auto-recovery classification is correct.
    * If the persisted reason is auto-recoverable (stale/legacy data — the
      reason should never have demanded manual rearm), it clears the flag
      instead of re-latching, so the platform boots NORMAL and stops
      re-firing ``ManualRearmRequired``.
    """
    try:
        from hft_platform.ops.manual_rearm import ManualRearmGate

        gate = ManualRearmGate()
        if not gate.requires_manual_rearm("platform"):
            return
        snapshot = gate.snapshot()
        platform = snapshot.get("platform") if isinstance(snapshot, dict) else None
        reason = (platform or {}).get("reason") or "restored_from_runtime_state"
        if reason in _AUTO_RECOVERABLE_REASONS:
            logger.warning(
                "platform_rearm_state_stale_auto_recoverable_cleared",
                reason=reason,
                note="auto-recoverable reason should never require manual rearm; clearing stale flag",
            )
            # Lock-free: we already hold _shared_controller_lock here.
            gate.clear_platform_flag()
            return
        controller.enter_reduce_only(reason=reason)
    except Exception as exc:
        logger.warning("platform_rearm_state_restore_failed", error=str(exc))


def reset_shared_platform_degrade_controller() -> None:
    global _shared_controller
    with _shared_controller_lock:
        _shared_controller = None
