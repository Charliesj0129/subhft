import asyncio
import os
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.recorder.audit import get_audit_writer

logger = get_logger("risk.storm_guard")


class StormGuardState(IntEnum):
    NORMAL = 0
    WARM = 1
    STORM = 2
    HALT = 3


@dataclass(slots=True)
class RiskThresholds:
    warm_drawdown_bps: int = -50  # -0.5% = -50 bps
    storm_drawdown_bps: int = -100  # -1.0% = -100 bps
    halt_drawdown_bps: int = -200  # -2.0% = -200 bps

    latency_warm_us: int = 5_000
    latency_storm_us: int = 20_000

    feed_gap_halt_s: float = 1.0  # precision-time


class StormGuard:
    """
    Central Risk Governance State Machine.
    Monitors System Health and Enforces Defcon Levels.
    """

    def __init__(
        self,
        thresholds: RiskThresholds | None = None,
        on_halt_callback: Callable[[], Any] | None = None,
    ):
        self.state = StormGuardState.NORMAL
        self.thresholds = thresholds or RiskThresholds()
        self._apply_env_overrides()
        self.metrics = MetricsRegistry.get()
        self.last_state_change = timebase.now_s()
        self._de_escalate_count: int = 0
        self._storm_entry_ts: float = 0.0  # precision-time
        self._storm_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "30"))  # precision-time
        self._de_escalate_threshold: int = int(os.getenv("HFT_STORMGUARD_DE_ESCALATE_N", "5"))
        self._on_halt_callback = on_halt_callback

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
        if "feed_gap_halt_s" in risk_cfg:
            self.thresholds.feed_gap_halt_s = float(risk_cfg["feed_gap_halt_s"])  # precision-ok
        self._apply_env_overrides()
        logger.info("StormGuard thresholds reloaded")

    def _apply_env_overrides(self) -> None:
        feed_gap_override = os.getenv("HFT_STORMGUARD_FEED_GAP_HALT_S")
        if feed_gap_override:
            try:
                self.thresholds.feed_gap_halt_s = float(feed_gap_override)  # precision-time
            except ValueError:
                logger.warning("Invalid HFT_STORMGUARD_FEED_GAP_HALT_S", value=feed_gap_override)

    def update(
        self, drawdown_bps: int = 0, latency_us: int = 0, feed_gap_s: float = 0.0
    ) -> StormGuardState:  # precision-ok
        """
        Evaluate inputs and transition state.
        Priority: HALT > STORM > WARM > NORMAL

        Args:
            drawdown_bps: Drawdown in basis points (1 bps = 0.01% = 0.0001).
                          E.g. -50 means -0.5%.
            latency_us: Latency in microseconds.
            feed_gap_s: Feed gap in seconds.
        """
        new_state = StormGuardState.NORMAL

        # 1. HALT Check
        if drawdown_bps <= self.thresholds.halt_drawdown_bps:
            new_state = StormGuardState.HALT
            reason = f"Drawdown {drawdown_bps}bps"

        # 2. STORM Check
        elif drawdown_bps <= self.thresholds.storm_drawdown_bps:
            new_state = StormGuardState.STORM
            reason = f"Drawdown {drawdown_bps}bps"
        elif latency_us >= self.thresholds.latency_storm_us:
            new_state = StormGuardState.STORM
            reason = f"Latency {latency_us}us"
        elif feed_gap_s >= self.thresholds.feed_gap_halt_s:
            # Keep feed gap as warning/storm signal but do not HALT on it.
            new_state = StormGuardState.STORM
            reason = f"Feed Gap {feed_gap_s:.3f}s"

        # 3. WARM Check
        elif drawdown_bps <= self.thresholds.warm_drawdown_bps:
            new_state = StormGuardState.WARM
            reason = "Drawdown Warning"
        elif latency_us >= self.thresholds.latency_warm_us:
            new_state = StormGuardState.WARM
            reason = "Latency Warning"

        # Transition Logic (with hysteresis protection for de-escalation)
        now = timebase.now_s()
        if new_state > self.state:
            # Escalation: always instant (safety-first)
            self._de_escalate_count = 0
            if new_state >= StormGuardState.STORM and self.state < StormGuardState.STORM:
                self._storm_entry_ts = now
            self.transition(new_state, reason)
        elif new_state < self.state:
            # Keep manual HALT recovery compatible with legacy tests/flows:
            # when all signals are clear, allow immediate step-down from HALT.
            if self.state == StormGuardState.HALT:
                self._de_escalate_count = 0
                self.transition(new_state, "Recovery")
                return self.state
            # De-escalation: requires (a) cooldown elapsed AND (b) N consecutive clear evals
            cooldown_ok = (
                (now - self._storm_entry_ts) >= self._storm_cooldown_s if self.state >= StormGuardState.STORM else True
            )
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
                        cooldown_s=self._storm_cooldown_s,
                        threshold=self._de_escalate_threshold,
                    )
            else:
                self._de_escalate_count = 0
        else:
            if new_state >= StormGuardState.STORM:
                self._de_escalate_count = 0

        return self.state

    def transition(self, new_state: StormGuardState, reason: str):
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

    def trigger_halt(self, reason: str):
        """Manual or Supervisor override to force HALT."""
        self.transition(StormGuardState.HALT, reason)

    def is_safe(self) -> bool:
        return self.state < StormGuardState.HALT
