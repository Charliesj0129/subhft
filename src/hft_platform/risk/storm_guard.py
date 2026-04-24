import asyncio
import os
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.drift_burst_detector import DriftBurstDetector

logger = get_logger("risk.storm_guard")

__all__ = ["StormGuard", "StormGuardState", "RiskThresholds"]


@dataclass(slots=True)
class RiskThresholds:
    warm_drawdown_bps: int = -50  # -0.5% = -50 bps
    storm_drawdown_bps: int = -100  # -1.0% = -100 bps
    halt_drawdown_bps: int = -200  # -2.0% = -200 bps

    latency_warm_us: int = 5_000
    latency_storm_us: int = 20_000

    feed_gap_storm_s: float = 1.0  # precision-time (triggers STORM, not HALT)


class StormGuard:
    """
    Central Risk Governance State Machine.
    Monitors System Health and Enforces Defcon Levels.
    """

    __slots__ = (
        "state",
        "thresholds",
        "metrics",
        "last_state_change",
        "_de_escalate_count",
        "_storm_entry_ts",
        "_storm_cooldown_s",
        "_halt_cooldown_s",
        "_halt_entry_ts",
        "_de_escalate_threshold",
        "_on_halt_callback",
        "_drift_burst_detector",
        "_state_lock",
        "_session_active",
        "_halt_exempt_strategies",
        "_position_provider",
        "_feature_failure_active",
        "_feature_failure_storm_ts",
        "_norm_failure_active",
        "_norm_failure_storm_ts",
        "_feed_gap_deescalation_ts",
        "_feed_gap_reescalation_cooldown_s",
        "_latency_deescalation_ts",
        "_latency_reescalation_cooldown_s",
        "_warm_deescalation_ts",
        "_warm_reescalation_cooldown_s",
        "_reconciliation_hold",
    )

    def __init__(
        self,
        thresholds: RiskThresholds | None = None,
        on_halt_callback: Callable[[], Any] | None = None,
        drift_burst_detector: DriftBurstDetector | None = None,
        halt_exempt_strategies: frozenset[str] | None = None,
        position_provider: Callable[[str, str], int] | None = None,
    ):
        self.state = StormGuardState.NORMAL
        self.thresholds = thresholds or RiskThresholds()
        self._apply_env_overrides()
        self.metrics = MetricsRegistry.get()
        self.last_state_change = time.monotonic()
        self._de_escalate_count: int = 0
        self._storm_entry_ts: float = 0.0  # precision-time
        self._storm_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "30"))  # precision-time
        self._halt_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_HALT_COOLDOWN_S", "60"))  # precision-time
        self._halt_entry_ts: float = 0.0  # precision-time
        self._de_escalate_threshold: int = int(os.getenv("HFT_STORMGUARD_DE_ESCALATE_N", "5"))
        self._on_halt_callback = on_halt_callback
        self._drift_burst_detector = drift_burst_detector
        self._state_lock = threading.Lock()
        self._session_active: bool = True  # default: active (safe)
        # Per-strategy HALT exemption: named strategies may bypass HALT/STORM blocking.
        # All other risk checks (position limits, exposure, etc.) still apply.
        env_exempt = os.getenv("HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES", "")
        env_set = frozenset(s.strip() for s in env_exempt.split(",") if s.strip()) if env_exempt else frozenset()
        self._halt_exempt_strategies: frozenset[str] = halt_exempt_strategies or env_set
        # Bug 21 (2026-04-17): allow reducing orders through HALT/STORM even when
        # strategy is not on exempt list. Prevents deadlock where strategy stuck
        # with open position can't cover because StormGuard is blocking.
        self._position_provider: Callable[[str, str], int] | None = position_provider
        self._feature_failure_active: bool = False
        self._feature_failure_storm_ts: float = 0.0
        self._norm_failure_active: bool = False
        self._norm_failure_storm_ts: float = 0.0
        self._feed_gap_deescalation_ts: float = 0.0  # noqa: monotonic timestamp
        self._feed_gap_reescalation_cooldown_s: float = float(  # noqa: duration
            os.getenv("HFT_STORMGUARD_FEED_GAP_REESCALATION_COOLDOWN_S", "120")
        )
        self._latency_deescalation_ts: float = 0.0
        self._latency_reescalation_cooldown_s: float = float(
            os.getenv("HFT_STORMGUARD_LATENCY_REESCALATION_COOLDOWN_S", "120")
        )
        # WARM re-escalation cooldown: suppresses repeated DriftBurst WARM
        # escalations immediately after WARM→NORMAL de-escalation. Prevents
        # 58/58 flip-flop observed when drift-burst toxicity oscillates near 0.5.
        self._warm_deescalation_ts: float = 0.0
        self._warm_reescalation_cooldown_s: float = float(
            os.getenv("HFT_STORMGUARD_WARM_REESCALATION_COOLDOWN_S", "300")
        )
        # Reconciliation hold: when True, HALT de-escalation is blocked until
        # ReconciliationService confirms drift is resolved.
        self._reconciliation_hold: bool = False

    def reload_thresholds(self, config: dict) -> None:
        """Update thresholds from new config."""
        with self._state_lock:
            risk_cfg = config.get("risk", config.get("global_defaults", {}))
            for key in (
                "warm_drawdown_bps",
                "storm_drawdown_bps",
                "halt_drawdown_bps",
                "latency_warm_us",
                "latency_storm_us",
            ):
                if key in risk_cfg:
                    setattr(self.thresholds, key, int(risk_cfg[key]))
            if "feed_gap_storm_s" in risk_cfg:
                self.thresholds.feed_gap_storm_s = float(risk_cfg["feed_gap_storm_s"])  # precision-ok
            self._apply_env_overrides()
        logger.info("StormGuard thresholds reloaded")

    def _apply_env_overrides(self) -> None:
        # Canonical env var (preferred)
        feed_gap_storm = os.getenv("HFT_STORMGUARD_FEED_GAP_STORM_S")
        # Deprecated alias (kept for backward compatibility)
        feed_gap_halt = os.getenv("HFT_STORMGUARD_FEED_GAP_HALT_S")

        if feed_gap_halt and not feed_gap_storm:
            logger.warning(
                "HFT_STORMGUARD_FEED_GAP_HALT_S is deprecated, use HFT_STORMGUARD_FEED_GAP_STORM_S",
                deprecated_var="HFT_STORMGUARD_FEED_GAP_HALT_S",
                value=feed_gap_halt,
            )
            feed_gap_storm = feed_gap_halt

        if feed_gap_storm:
            try:
                self.thresholds.feed_gap_storm_s = float(feed_gap_storm)  # precision-time
            except ValueError:
                logger.warning(
                    "Invalid feed gap storm threshold",
                    var="HFT_STORMGUARD_FEED_GAP_STORM_S",
                    value=feed_gap_storm,
                )

        latency_storm = os.getenv("HFT_STORMGUARD_LATENCY_STORM_US")
        if latency_storm:
            try:
                self.thresholds.latency_storm_us = int(latency_storm)
            except ValueError:
                logger.warning("Invalid HFT_STORMGUARD_LATENCY_STORM_US", value=latency_storm)

        latency_warm = os.getenv("HFT_STORMGUARD_LATENCY_WARM_US")
        if latency_warm:
            try:
                self.thresholds.latency_warm_us = int(latency_warm)
            except ValueError:
                logger.warning("Invalid HFT_STORMGUARD_LATENCY_WARM_US", value=latency_warm)

    def _evaluate_target_state(
        self,
        drawdown_bps: int,
        latency_us: int,
        feed_gap_s: float,  # precision-time (not a price; seconds, float acceptable)
    ) -> tuple[StormGuardState, str]:
        """Determine target state from inputs. Priority: HALT > STORM > WARM > NORMAL."""
        t = self.thresholds
        if drawdown_bps <= t.halt_drawdown_bps:
            return StormGuardState.HALT, f"Drawdown {drawdown_bps}bps"
        if drawdown_bps <= t.storm_drawdown_bps:
            return StormGuardState.STORM, f"Drawdown {drawdown_bps}bps"
        if latency_us >= t.latency_storm_us:
            return StormGuardState.STORM, f"Latency {latency_us}us"
        if feed_gap_s >= t.feed_gap_storm_s and self._session_active:
            return StormGuardState.STORM, f"Feed Gap {feed_gap_s:.3f}s"
        # Component failure holds STORM regardless of drawdown/latency WARM thresholds.
        # Must be checked before WARM returns so that STORM persists even when
        # drawdown/latency are in the WARM range (not STORM range).
        norm_fail = self._norm_failure_active
        feat_fail = self._feature_failure_active
        if norm_fail or feat_fail:
            if norm_fail and feat_fail:
                return StormGuardState.STORM, "Component failure active (norm+feature)"
            if norm_fail:
                return StormGuardState.STORM, "Normalizer failure active"
            return StormGuardState.STORM, "FeatureEngine failure active"
        if drawdown_bps <= t.warm_drawdown_bps:
            return StormGuardState.WARM, "Drawdown Warning"
        if latency_us >= t.latency_warm_us:
            return StormGuardState.WARM, "Latency Warning"
        return StormGuardState.NORMAL, ""

    def update(
        self,
        drawdown_bps: int = 0,
        latency_us: int = 0,
        feed_gap_s: float = 0.0,  # precision-ok
    ) -> StormGuardState:
        """
        Evaluate inputs and transition state.

        Args:
            drawdown_bps: Drawdown in basis points (1 bps = 0.01% = 0.0001).
            latency_us: Latency in microseconds.
            feed_gap_s: Feed gap in seconds.
        """
        new_state, reason = self._evaluate_target_state(drawdown_bps, latency_us, feed_gap_s)

        # Transition Logic (with hysteresis protection for de-escalation)
        fire_callback = False
        current_state: StormGuardState = self.state
        with self._state_lock:
            now = time.monotonic()
            if new_state > self.state:
                # Re-escalation suppression: after de-escalating from STORM,
                # suppress re-triggers for a cooldown period to prevent flapping
                # (observed: 75 transitions/6hr in production for feed gap).
                _suppress = False
                if (
                    reason.startswith("Feed Gap")
                    and self._feed_gap_deescalation_ts > 0
                    and (now - self._feed_gap_deescalation_ts) < self._feed_gap_reescalation_cooldown_s
                ):
                    _suppress = True
                elif (
                    reason.startswith("Latency")
                    and self._latency_deescalation_ts > 0
                    and (now - self._latency_deescalation_ts) < self._latency_reescalation_cooldown_s
                ):
                    _suppress = True
                if _suppress:
                    pass  # suppress — stay at current state
                else:
                    # Escalation: always instant (safety-first)
                    self._de_escalate_count = 0
                    if new_state >= StormGuardState.STORM and self.state < StormGuardState.STORM:
                        self._storm_entry_ts = now
                    if new_state == StormGuardState.HALT:
                        self._halt_entry_ts = now
                    _, fire_callback = self._transition(new_state, reason)
            elif new_state < self.state:
                # Reconciliation hold: block de-escalation from HALT while
                # position drift is unresolved (prevents auto-recovery that
                # leaves the system trading with stale positions).
                if self.state == StormGuardState.HALT and self._reconciliation_hold:
                    self._de_escalate_count = 0
                    logger.warning(
                        "stormguard_deescalation_blocked_reconciliation_hold",
                        current_state=self.state.name,
                        target_state=new_state.name,
                    )
                    current_state = self.state
                    return current_state
                # De-escalation from any elevated state requires cooldown + N consecutive clears
                if self.state == StormGuardState.HALT:
                    cooldown_ok = (now - self._halt_entry_ts) >= self._halt_cooldown_s
                elif self.state >= StormGuardState.STORM:
                    cooldown_ok = (now - self._storm_entry_ts) >= self._storm_cooldown_s
                else:
                    cooldown_ok = True

                if cooldown_ok:
                    self._de_escalate_count += 1
                    if self._de_escalate_count >= self._de_escalate_threshold:
                        old_for_log = self.state
                        self._de_escalate_count = 0
                        # Record de-escalation from STORM for re-escalation cooldown
                        if old_for_log >= StormGuardState.STORM:
                            self._feed_gap_deescalation_ts = now
                            self._latency_deescalation_ts = now
                        # Record WARM→lower de-escalation for drift-burst re-escalation cooldown
                        if old_for_log == StormGuardState.WARM and new_state < StormGuardState.WARM:
                            self._warm_deescalation_ts = now
                        # Reset storm entry timestamp so next STORM gets fresh cooldown
                        self._storm_entry_ts = 0.0
                        _, fire_callback = self._transition(new_state, "Recovery")
                        logger.info(
                            "StormGuard de-escalated after hysteresis",
                            from_state=old_for_log.name,
                            to_state=new_state.name,
                            cooldown_s=self._halt_cooldown_s
                            if old_for_log == StormGuardState.HALT
                            else self._storm_cooldown_s,
                            threshold=self._de_escalate_threshold,
                        )
                else:
                    self._de_escalate_count = 0
            else:
                if new_state >= StormGuardState.STORM:
                    self._de_escalate_count = 0

            current_state = self.state

        if fire_callback:
            self._fire_halt_callback()

        return current_state

    def update_with_lob(
        self,
        mid_price_x2: int,
        spread_scaled: int = 0,
        imbalance: float = 0.0,
        ts: int = 0,
        symbol: str = "",
    ) -> StormGuardState:
        """Evaluate LOB-derived drift-burst toxicity and escalate state if needed.

        This method is additive-only: it can escalate the StormGuard state but
        never de-escalate it. If no DriftBurstDetector is configured, this is
        a no-op that returns the current state.

        Args:
            mid_price_x2: best_bid + best_ask (scaled int x10000).
            spread_scaled: best_ask - best_bid (scaled int x10000).
            imbalance: LOB imbalance ratio [-1, 1].
            ts: Timestamp in nanoseconds.
            symbol: Symbol driving this LOB update (for observability only).

        Returns:
            Current StormGuardState after potential escalation.
        """
        # Crossed book (spread < 0) or empty book (mid_price_x2 <= 0) indicates
        # data corruption or exchange anomaly. Escalate to STORM immediately.
        if spread_scaled < 0:
            with self._state_lock:
                if self.state < StormGuardState.STORM:
                    self._transition(
                        StormGuardState.STORM,
                        f"Crossed book: spread_scaled={spread_scaled}",
                    )
            return self.state
        if mid_price_x2 <= 0:
            # Empty/invalid book — skip DriftBurst to avoid feeding invalid data
            return self.state

        detector = self._drift_burst_detector
        if detector is None:
            return self.state

        result = detector.evaluate(mid_price_x2, spread_scaled, imbalance, ts)

        # Determine escalation target from toxicity signal
        # Only escalate, never de-escalate (additive safety)
        if result.burst_detected and result.toxicity_score > 0.9:
            target = StormGuardState.HALT
            reason = f"DriftBurst HALT: toxicity={result.toxicity_score:.3f}"
        elif result.toxicity_score > 0.8:
            target = StormGuardState.STORM
            reason = f"DriftBurst STORM: toxicity={result.toxicity_score:.3f}"
        elif result.toxicity_score > 0.5:
            target = StormGuardState.WARM
            reason = f"DriftBurst WARM: toxicity={result.toxicity_score:.3f}"
        else:
            return self.state

        # Only escalate — never de-escalate from drift burst
        fire_callback = False
        with self._state_lock:
            # WARM re-escalation cooldown: suppress if we recently de-escalated
            # from WARM. Prevents 58/58 flip-flop when toxicity oscillates near
            # the 0.5 boundary. Higher states (STORM/HALT) bypass this guard.
            now = time.monotonic()
            if (
                target == StormGuardState.WARM
                and self.state < StormGuardState.WARM
                and self._warm_deescalation_ts > 0
                and (now - self._warm_deescalation_ts) < self._warm_reescalation_cooldown_s
            ):
                return self.state
            if target > self.state:
                self._de_escalate_count = 0
                if target >= StormGuardState.STORM and self.state < StormGuardState.STORM:
                    self._storm_entry_ts = now
                if target == StormGuardState.HALT:
                    self._halt_entry_ts = now
                _, fire_callback = self._transition(target, reason)
                logger.info(
                    "StormGuard drift_burst escalation",
                    symbol=symbol,
                    new_state=target.name,
                    toxicity_score=f"{result.toxicity_score:.3f}",
                    burst_detected=result.burst_detected,
                )

            current_state = self.state

        if fire_callback:
            self._fire_halt_callback()

        return current_state

    def _transition(self, new_state: StormGuardState, reason: str) -> tuple[StormGuardState, bool]:
        """Transition state machine. Returns (old_state, should_fire_halt_callback).

        IMPORTANT: Caller must invoke _fire_halt_callback() AFTER releasing
        _state_lock when the second element is True.
        """
        old_state = self.state
        self.state = new_state
        self.last_state_change = time.monotonic()

        logger.warning("StormGuard Transition", old=old_state.name, new=new_state.name, reason=reason)

        # Update Metric — log at WARNING if metrics fail during a state
        # transition so operators know observability is degraded (INFRA-011).
        try:
            self.metrics.stormguard_mode.labels(strategy="system").set(int(new_state))
        except Exception as exc:
            logger.warning("stormguard_metric_update_failed", metric="mode", error=str(exc))

        # Count transition direction
        direction = "escalation" if int(new_state) > int(old_state) else "de_escalation"
        try:
            self.metrics.stormguard_transitions_total.labels(direction=direction).inc()
        except Exception as exc:
            logger.warning("stormguard_metric_update_failed", metric="transitions", error=str(exc))

        # Audit guardrail transition
        try:
            from hft_platform.recorder.audit import get_audit_writer

            audit = get_audit_writer()
            audit.log_guardrail_transition(
                {
                    "old_state": old_state.name,
                    "new_state": new_state.name,
                    "reason": reason,
                }
            )
        except Exception as exc:
            logger.warning("audit_guardrail_transition_failed", error=str(exc))

        # Signal caller to fire callback OUTSIDE the lock
        # Only fire on *entry* to HALT, not re-entry (prevents infinite recursion)
        should_fire = (
            new_state == StormGuardState.HALT
            and old_state != StormGuardState.HALT
            and self._on_halt_callback is not None
        )
        return old_state, should_fire

    def _fire_halt_callback(self) -> None:
        """Invoke the on_halt_callback. Must be called OUTSIDE _state_lock."""
        if self._on_halt_callback is None:
            return
        try:
            result = self._on_halt_callback()
            # If callback is a coroutine, schedule it thread-safely
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                    future = asyncio.run_coroutine_threadsafe(result, loop)
                    future.add_done_callback(self._halt_callback_done)
                except RuntimeError:
                    # No running event loop; close the coroutine explicitly so
                    # the interpreter does not emit an unawaited-coroutine warning.
                    result.close()
                    logger.warning("halt_callback_coroutine_no_loop")
        except Exception as exc:
            logger.error(
                "on_halt_callback_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    @staticmethod
    def _halt_callback_done(future: Future[Any]) -> None:
        """Log errors from fire-and-forget halt callback futures."""
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.error(
                "halt_callback_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def trigger_storm(self, reason: str) -> None:
        """Escalate to STORM state. Less severe than trigger_halt -- used for
        transient backpressure conditions that may self-resolve."""
        with self._state_lock:
            if self.state < StormGuardState.STORM:
                now = time.monotonic()
                self._storm_entry_ts = now
                self._de_escalate_count = 0
                self._transition(StormGuardState.STORM, reason)

    def trigger_halt(self, reason: str) -> None:
        """Manual or Supervisor override to force HALT."""
        fire_callback = False
        with self._state_lock:
            now = time.monotonic()
            if self.state < StormGuardState.STORM:
                self._storm_entry_ts = now
            self._halt_entry_ts = now
            self._de_escalate_count = 0
            _, fire_callback = self._transition(StormGuardState.HALT, reason)

        if fire_callback:
            self._fire_halt_callback()

    def validate(self, intent: OrderIntent) -> tuple[bool, str]:
        with self._state_lock:
            return self._validate_locked(intent)

    def _validate_locked(self, intent: OrderIntent) -> tuple[bool, str]:
        """H4: policy body for validate / check_and_submit. Caller MUST
        hold ``self._state_lock``. Split out so ``check_and_submit`` can
        run the validation and the downstream queue-put under one lock
        acquisition, closing the race between the state read and the put.
        """
        if self.state == StormGuardState.HALT:
            if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
                return True, "OK"
            if self._intent_reduces_position(intent):
                return True, "HALT_REDUCE_ONLY"
            if intent.strategy_id in self._halt_exempt_strategies:
                logger.warning(
                    "stormguard_halt_exempt_bypass",
                    strategy_id=intent.strategy_id,
                    intent_type=intent.intent_type.name,
                    symbol=intent.symbol,
                )
                try:
                    self.metrics.stormguard_halt_exempt_bypass_total.inc()
                except Exception:
                    pass
                return True, "HALT_EXEMPT"
            return False, "STORMGUARD_HALT"
        if self.state == StormGuardState.STORM:
            if intent.intent_type in (IntentType.NEW, IntentType.AMEND):
                if self._intent_reduces_position(intent):
                    return True, "STORM_REDUCE_ONLY"
                if intent.strategy_id in self._halt_exempt_strategies:
                    logger.warning(
                        "stormguard_storm_exempt_bypass",
                        strategy_id=intent.strategy_id,
                        symbol=intent.symbol,
                    )
                    return True, "STORM_EXEMPT"
                return False, "STORMGUARD_STORM_BLOCKED"
        return True, "OK"

    def check_and_submit(
        self,
        intent: OrderIntent,
        submit_fn: Callable[[], Any],
    ) -> tuple[bool, str]:
        """H4: atomic HALT check + submit. Acquires ``_state_lock`` once,
        runs validation, and — if approved — calls ``submit_fn`` while
        the lock is still held. A concurrent ``trigger_halt`` blocks on
        the lock; either it preempts (intent sees HALT) or it runs after
        submit_fn returned (submission completes before HALT). Either
        ordering is consistent, eliminating the post-approve race that
        the 3-layer gate patched at the cost of inconsistent telemetry.

        ``submit_fn`` must be non-blocking (e.g. ``queue.put_nowait``);
        blocking inside the lock would stall all other StormGuard
        transitions.
        """
        with self._state_lock:
            ok, reason = self._validate_locked(intent)
            if ok:
                submit_fn()
            return ok, reason

    def set_position_provider(self, provider: Callable[[str, str], int] | None) -> None:
        """Bug 21: wire position provider post-construction.

        Bootstrap creates StormGuard before the position store is available.
        RiskEngine injects its own ``_current_strategy_symbol_net_position``
        here so reducing orders can bypass HALT/STORM safely.
        """
        self._position_provider = provider

    def _intent_reduces_position(self, intent: OrderIntent) -> bool:
        """Bug 21 helper: True iff intent strictly reduces |net_position|.

        When ``position_provider`` is not wired (e.g. unit tests), returns False
        — conservative default that preserves the original HALT blocking.
        """
        provider = self._position_provider
        if provider is None:
            return False
        try:
            current = int(provider(intent.symbol, intent.strategy_id) or 0)
        except Exception:  # noqa: BLE001
            return False
        if current == 0:
            return False
        signed = int(intent.qty if intent.side == Side.BUY else -intent.qty)
        return abs(current + signed) < abs(current)

    def set_session_active(self, active: bool) -> None:
        """Inform StormGuard whether any trading session is currently open.

        When no session is active, feed gap evaluation is suppressed to avoid
        spurious STORM transitions during expected inter-session breaks.
        """
        self._session_active = active

    def report_feature_failure(self, count: int) -> None:
        """Escalate to STORM when FeatureEngine has consecutive failures.

        This is a targeted escalation: it sets ``_feature_failure_active`` so
        that :meth:`report_feature_recovery` can clear the condition.  If the
        system is already at STORM or higher for another reason, this is a
        no-op on state but still marks the feature-failure flag.
        """
        with self._state_lock:
            self._feature_failure_active = True
            self._feature_failure_storm_ts = time.monotonic()
            if self.state < StormGuardState.STORM:
                self._de_escalate_count = 0
                self._storm_entry_ts = self._feature_failure_storm_ts
                self._transition(
                    StormGuardState.STORM,
                    f"FeatureEngine consecutive failures: {count}",
                )
        try:
            self.metrics.feature_engine_escalation_total.inc()
        except Exception:
            pass
        logger.warning(
            "stormguard_feature_failure_escalation",
            consecutive_failures=count,
        )

    _FEATURE_RECOVERY_HOLD_S: float = float(os.getenv("HFT_STORMGUARD_FEATURE_RECOVERY_HOLD_S", "5"))  # noqa: precision-ok (duration, not financial)

    def report_feature_recovery(self) -> None:
        """Clear feature-failure flag after FeatureEngine recovers.

        Does NOT transition state directly — the next ``update()`` cycle will
        re-evaluate all conditions (latency, drawdown, feed gap) and
        de-escalate only if ALL reasons have cleared.  This prevents the bug
        where feature recovery incorrectly de-escalates STORM caused by
        multiple concurrent conditions.

        Anti-flap: recovery is suppressed if less than
        ``_FEATURE_RECOVERY_HOLD_S`` seconds have passed since the last
        feature-failure escalation.
        """
        with self._state_lock:
            if not self._feature_failure_active:
                return
            # Anti-flap: hold STORM for a minimum period
            elapsed = time.monotonic() - self._feature_failure_storm_ts
            if elapsed < self._FEATURE_RECOVERY_HOLD_S:
                logger.debug(
                    "stormguard_feature_recovery_suppressed",
                    elapsed_s=round(elapsed, 2),
                    hold_s=self._FEATURE_RECOVERY_HOLD_S,
                )
                return
            self._feature_failure_active = False
            # Don't transition — let update() handle de-escalation so that
            # other active STORM conditions (latency, drawdown) are respected.
        logger.info("feature_engine_recovered_flag_cleared")

    def report_norm_failure(self, count: int) -> None:
        """Escalate to STORM when normalizer has consecutive failures.

        Mirrors :meth:`report_feature_failure` for the normalizer domain.
        Sets ``_norm_failure_active`` independently of feature-failure flag.
        """
        with self._state_lock:
            self._norm_failure_active = True
            self._norm_failure_storm_ts = time.monotonic()
            if self.state < StormGuardState.STORM:
                self._de_escalate_count = 0
                self._storm_entry_ts = self._norm_failure_storm_ts
                self._transition(
                    StormGuardState.STORM,
                    f"Normalizer consecutive failures: {count}",
                )
        try:
            self.metrics.norm_engine_escalation_total.inc()
        except Exception:
            pass
        logger.warning(
            "stormguard_norm_failure_escalation",
            consecutive_failures=count,
        )

    def report_norm_recovery(self) -> None:
        """Clear normalizer-failure flag after normalizer recovers.

        Mirrors :meth:`report_feature_recovery` for the normalizer domain.
        Anti-flap hold period applies independently.
        """
        with self._state_lock:
            if not self._norm_failure_active:
                return
            elapsed = time.monotonic() - self._norm_failure_storm_ts
            if elapsed < self._FEATURE_RECOVERY_HOLD_S:
                logger.debug(
                    "stormguard_norm_recovery_suppressed",
                    elapsed_s=round(elapsed, 2),
                    hold_s=self._FEATURE_RECOVERY_HOLD_S,
                )
                return
            self._norm_failure_active = False
        logger.info("normalizer_recovered_flag_cleared")

    def is_halt_exempt(self, strategy_id: str) -> bool:
        """Public API: check if a strategy is halt-exempt."""
        return strategy_id in self._halt_exempt_strategies

    def revoke_halt_exemption(self, strategy_id: str) -> bool:
        """Runtime kill switch: revoke halt-exempt status for a strategy.

        Thread-safe. Uses frozenset replacement under lock.
        """
        with self._state_lock:
            if strategy_id in self._halt_exempt_strategies:
                self._halt_exempt_strategies = self._halt_exempt_strategies - {strategy_id}
                logger.warning("halt_exempt_revoked", strategy_id=strategy_id)
                return True
            return False

    _MAX_HALT_EXEMPT_STRATEGIES = 50

    def grant_halt_exemption(self, strategy_id: str) -> bool:
        """Runtime grant: add halt-exempt status (audit logged).

        Thread-safe. Uses frozenset replacement under lock.
        Returns False if cardinality limit (_MAX_HALT_EXEMPT_STRATEGIES) reached.
        """
        with self._state_lock:
            if len(self._halt_exempt_strategies) >= self._MAX_HALT_EXEMPT_STRATEGIES:
                logger.warning(
                    "halt_exempt_grant_rejected_cardinality",
                    strategy_id=strategy_id,
                    current=len(self._halt_exempt_strategies),
                    limit=self._MAX_HALT_EXEMPT_STRATEGIES,
                )
                return False
            self._halt_exempt_strategies = self._halt_exempt_strategies | {strategy_id}
            logger.warning("halt_exempt_granted", strategy_id=strategy_id)
            return True

    def is_safe(self) -> bool:
        return self.state < StormGuardState.HALT

    def set_reconciliation_hold(self, hold: bool) -> None:
        """Set or clear reconciliation hold on HALT de-escalation.

        When ``hold=True``, HALT will not auto-recover via ``update()`` until
        ReconciliationService confirms drift is resolved (calls with ``hold=False``).
        Thread-safe.
        """
        with self._state_lock:
            old = self._reconciliation_hold
            self._reconciliation_hold = hold
        if old != hold:
            logger.warning(
                "stormguard_reconciliation_hold_changed",
                hold=hold,
                current_state=self.state.name,
            )

    @property
    def reconciliation_hold(self) -> bool:
        """Whether reconciliation hold is active (read-only)."""
        return self._reconciliation_hold
