import asyncio
import os
from dataclasses import dataclass
from typing import Any, Callable

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, StormGuardState
from hft_platform.core import timebase
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.recorder.audit import get_audit_writer
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
    )

    def __init__(
        self,
        thresholds: RiskThresholds | None = None,
        on_halt_callback: Callable[[], Any] | None = None,
        drift_burst_detector: DriftBurstDetector | None = None,
    ):
        self.state = StormGuardState.NORMAL
        self.thresholds = thresholds or RiskThresholds()
        self._apply_env_overrides()
        self.metrics = MetricsRegistry.get()
        self.last_state_change = timebase.now_s()
        self._de_escalate_count: int = 0
        self._storm_entry_ts: float = 0.0  # precision-time
        self._storm_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "30"))  # precision-time
        self._halt_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_HALT_COOLDOWN_S", "60"))  # precision-time
        self._halt_entry_ts: float = 0.0  # precision-time
        self._de_escalate_threshold: int = int(os.getenv("HFT_STORMGUARD_DE_ESCALATE_N", "5"))
        self._on_halt_callback = on_halt_callback
        self._drift_burst_detector = drift_burst_detector

    def reload_thresholds(self, config: dict) -> None:
        """Update thresholds from new config."""
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
        feed_gap_override = os.getenv("HFT_STORMGUARD_FEED_GAP_HALT_S")
        if feed_gap_override:
            try:
                self.thresholds.feed_gap_storm_s = float(feed_gap_override)  # precision-time
            except ValueError:
                logger.warning("Invalid HFT_STORMGUARD_FEED_GAP_HALT_S", value=feed_gap_override)

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
        if feed_gap_s >= t.feed_gap_storm_s:
            return StormGuardState.STORM, f"Feed Gap {feed_gap_s:.3f}s"
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
        now = timebase.now_s()
        if new_state > self.state:
            # Escalation: always instant (safety-first)
            self._de_escalate_count = 0
            if new_state >= StormGuardState.STORM and self.state < StormGuardState.STORM:
                self._storm_entry_ts = now
            if new_state == StormGuardState.HALT:
                self._halt_entry_ts = now
            self.transition(new_state, reason)
        elif new_state < self.state:
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
                    self.transition(new_state, "Recovery")
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

        return self.state

    def update_with_lob(
        self,
        mid_price_x2: int,
        spread_scaled: int = 0,
        imbalance: float = 0.0,
        ts: int = 0,
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

        Returns:
            Current StormGuardState after potential escalation.
        """
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
        if target > self.state:
            now = timebase.now_s()
            self._de_escalate_count = 0
            if target >= StormGuardState.STORM and self.state < StormGuardState.STORM:
                self._storm_entry_ts = now
            if target == StormGuardState.HALT:
                self._halt_entry_ts = now
            self.transition(target, reason)
            logger.info(
                "StormGuard drift_burst escalation",
                new_state=target.name,
                toxicity_score=f"{result.toxicity_score:.3f}",
                burst_detected=result.burst_detected,
            )

        return self.state

    def transition(self, new_state: StormGuardState, reason: str) -> None:
        old_state = self.state
        self.state = new_state
        self.last_state_change = timebase.now_s()

        logger.warning("StormGuard Transition", old=old_state.name, new=new_state.name, reason=reason)

        # Update Metric
        self.metrics.stormguard_mode.labels(strategy="system").set(int(new_state))

        # Audit guardrail transition
        try:
            audit = get_audit_writer()
            audit.log_guardrail_transition(
                {
                    "old_state": old_state.name,
                    "new_state": new_state.name,
                    "reason": reason,
                }
            )
        except Exception as exc:
            logger.debug("audit_guardrail_transition_failed", error=str(exc))

        # Fire on_halt_callback when entering HALT
        if new_state == StormGuardState.HALT and self._on_halt_callback is not None:
            try:
                result = self._on_halt_callback()
                # If callback is a coroutine, schedule it
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)
                    except RuntimeError:
                        # No running event loop; log and discard
                        logger.warning("halt_callback_coroutine_no_loop")
            except Exception as exc:
                logger.error(
                    "on_halt_callback_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    def trigger_halt(self, reason: str) -> None:
        """Manual or Supervisor override to force HALT."""
        self.transition(StormGuardState.HALT, reason)

    def validate(self, intent: OrderIntent) -> tuple[bool, str]:
        if self.state == StormGuardState.HALT:
            if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
                return True, "OK"
            return False, "STORMGUARD_HALT"
        if self.state == StormGuardState.STORM:
            if intent.intent_type == IntentType.NEW:
                return False, "STORMGUARD_STORM_NEW_BLOCKED"
        return True, "OK"

    def is_safe(self) -> bool:
        return self.state < StormGuardState.HALT
