import asyncio
import os
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.drift_burst_detector import DriftBurstDetector

logger = get_logger("risk.storm_guard")

__all__ = ["StormGuard", "StormGuardState", "RiskThresholds", "halt_reason_label"]

_HALT_REASON_LABEL_CAP = 32
_HALT_REASON_LABEL_MAX_LEN = 48
_halt_reason_labels_seen: set[str] = set()


def halt_reason_label(reason: str) -> str:
    """Bounded metric label for a trigger_halt reason.

    The text before the first ':' names the cause ("KILL_SWITCH_FILE",
    "raw_queue_sustained_drops"); what follows is per-incident detail (counts,
    file contents) that would make every HALT a new series.
    """
    label = str(reason).split(":", 1)[0].strip()[:_HALT_REASON_LABEL_MAX_LEN] or "unspecified"
    if label in _halt_reason_labels_seen:
        return label
    if len(_halt_reason_labels_seen) >= _HALT_REASON_LABEL_CAP:
        return "other"
    _halt_reason_labels_seen.add(label)
    return label


@dataclass(slots=True)
class RiskThresholds:
    latency_warm_us: int = 5_000
    latency_storm_us: int = 20_000

    order_rtt_warm_us: int = 1_500_000
    order_rtt_storm_us: int = 0
    order_rtt_storm_count: int = 3
    order_rtt_storm_window_s: float = 60.0
    order_rtt_storm_dwell_s: float = 300.0

    feed_gap_storm_s: float = 1.0

    broker_backpressure_silence_us: int = 1_000_000
    broker_stall_storm_us: int = 10_000_000


_INT_US_KEYS: tuple[str, ...] = (
    "latency_warm_us",
    "latency_storm_us",
    "order_rtt_warm_us",
    "order_rtt_storm_us",
    "broker_backpressure_silence_us",
    "broker_stall_storm_us",
)
STORMGUARD_CONFIG_KEYS: frozenset[str] = frozenset(
    _INT_US_KEYS + ("order_rtt_storm_count", "order_rtt_storm_window_s", "order_rtt_storm_dwell_s", "feed_gap_storm_s")
)


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
        "_warm_entry_ts",
        "_warm_cooldown_s",
        "_warm_toxicity_entry",
        "_warm_toxicity_exit",
        "_de_escalate_threshold",
        "_on_halt_callback",
        "_drift_burst_detector",
        "_state_lock",
        "_session_active",
        "_halt_exempt_strategies",
        "_position_provider",
        "_target_state_reason",
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
        "_daily_loss_hold",
        "_kill_switch_hold",
        "_manual_latch_hold",
        "_loop",
        "_pending_transition_emit",
        "_inflight_dispatch_tickets",
        "_next_ticket_id",
        "_order_rtt_peak_us",
        "_order_rtt_slow_ts",
        "_order_rtt_slow_fresh",
        "_order_rtt_storm_on",
        "_halt_reason",
        "_halt_reason_label",
        "_halt_label_lock",
        "_broker_stall_provider",
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
        self.metrics = MetricsRegistry.get()
        self._order_rtt_slow_ts: deque[float] = deque(maxlen=1)
        self._order_rtt_slow_fresh: bool = False
        self._order_rtt_storm_on: bool = False
        self._broker_stall_provider: Callable[[], int] | None = None
        self._apply_env_overrides()
        self.last_state_change = time.monotonic()
        self._de_escalate_count: int = 0
        self._storm_entry_ts: float = 0.0
        self._storm_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "30"))
        self._halt_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_HALT_COOLDOWN_S", "60"))
        self._halt_entry_ts: float = 0.0
        self._warm_entry_ts: float = 0.0
        self._warm_cooldown_s: float = float(os.getenv("HFT_STORMGUARD_WARM_COOLDOWN_S", "30"))
        self._warm_toxicity_entry: float = float(os.getenv("HFT_STORMGUARD_WARM_TOXICITY_ENTRY", "0.5"))
        self._warm_toxicity_exit: float = float(os.getenv("HFT_STORMGUARD_WARM_TOXICITY_EXIT", "0.4"))
        self._de_escalate_threshold: int = int(os.getenv("HFT_STORMGUARD_DE_ESCALATE_N", "5"))
        self._on_halt_callback = on_halt_callback
        self._drift_burst_detector = drift_burst_detector
        self._state_lock = threading.Lock()
        self._session_active: bool = True
        env_exempt = os.getenv("HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES", "")
        env_set = frozenset(s.strip() for s in env_exempt.split(",") if s.strip()) if env_exempt else frozenset()
        self._halt_exempt_strategies: frozenset[str] = halt_exempt_strategies or env_set
        self._position_provider: Callable[[str, str], int] | None = position_provider
        self._feature_failure_active: bool = False
        self._feature_failure_storm_ts: float = 0.0
        self._norm_failure_active: bool = False
        self._norm_failure_storm_ts: float = 0.0
        self._feed_gap_deescalation_ts: float = 0.0
        self._feed_gap_reescalation_cooldown_s: float = float(
            os.getenv("HFT_STORMGUARD_FEED_GAP_REESCALATION_COOLDOWN_S", "120")
        )
        self._latency_deescalation_ts: float = 0.0
        self._latency_reescalation_cooldown_s: float = float(
            os.getenv("HFT_STORMGUARD_LATENCY_REESCALATION_COOLDOWN_S", "120")
        )
        self._warm_deescalation_ts: float = 0.0
        self._warm_reescalation_cooldown_s: float = float(
            os.getenv("HFT_STORMGUARD_WARM_REESCALATION_COOLDOWN_S", "300")
        )
        self._reconciliation_hold: bool = False
        self._daily_loss_hold: bool = False
        self._kill_switch_hold: bool = False
        self._manual_latch_hold: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_transition_emit: list[dict[str, Any]] = []
        self._inflight_dispatch_tickets: dict[int, dict[str, Any]] = {}
        self._next_ticket_id: int = 0
        self._order_rtt_peak_us: int = 0
        self._halt_reason: str = ""
        self._halt_reason_label: str = ""
        self._halt_label_lock = threading.Lock()
        self._target_state_reason: str = ""

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the engine event loop for cross-thread halt-callback dispatch.

        MUST be called once from the engine loop (HFTSystem.run()) before any
        non-asyncio thread can trigger HALT via ``trigger_halt`` or similar.
        Without this, a coroutine halt-callback fired from a daemon thread
        cannot be scheduled and is silently closed — Telegram / audit alerts
        for session-lease loss (bootstrap lease-refresh thread) are dropped.

        Idempotent and safe to call multiple times; the last binding wins.
        """
        self._loop = loop

    def get_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the bound engine event loop, or ``None`` if not yet bound.

        H2 (2026-04-25): exposed publicly so bootstrap-built notification
        callbacks can lazy-resolve the engine loop reference without poking
        ``_loop`` directly. Bootstrap creates the callback during ``build()``
        when ``HFTSystem.run()`` has not yet bound the loop; the closure must
        re-read ``get_loop()`` on every fire so the first HALT does not
        observe a stale ``None``.
        """
        return self._loop

    @staticmethod
    def config_section(config: dict) -> dict:
        """The mapping ``reload_thresholds`` reads its keys from."""
        section = config.get("risk", config.get("global_defaults", {})) if isinstance(config, dict) else {}
        return section if isinstance(section, dict) else {}

    @classmethod
    def config_has_thresholds(cls, config: dict) -> bool:
        """True iff ``config`` sets any key ``reload_thresholds`` applies."""
        section = cls.config_section(config)
        return any(key in section for key in STORMGUARD_CONFIG_KEYS)

    def reload_thresholds(self, config: dict) -> None:
        """Update thresholds from new config; env overrides are re-applied on top.

        Every key is parsed before any is applied, so one bad value leaves all
        thresholds (and the env overrides on them) as they were.
        """
        risk_cfg = self.config_section(config)
        parsed: dict[str, int | float] = {}
        for key in _INT_US_KEYS:
            if key in risk_cfg:
                parsed[key] = int(risk_cfg[key])
        if "order_rtt_storm_count" in risk_cfg:
            parsed["order_rtt_storm_count"] = max(1, int(risk_cfg["order_rtt_storm_count"]))
        for key in ("order_rtt_storm_window_s", "order_rtt_storm_dwell_s", "feed_gap_storm_s"):
            if key in risk_cfg:
                parsed[key] = float(risk_cfg[key])
        with self._state_lock:
            for key, value in parsed.items():
                setattr(self.thresholds, key, value)
            self._apply_env_overrides()
        logger.info("StormGuard thresholds reloaded")

    def _apply_env_overrides(self) -> None:
        feed_gap_storm = os.getenv("HFT_STORMGUARD_FEED_GAP_STORM_S")
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
                self.thresholds.feed_gap_storm_s = float(feed_gap_storm)
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

        for var, attr in (
            ("HFT_STORMGUARD_ORDER_RTT_WARM_US", "order_rtt_warm_us"),
            ("HFT_STORMGUARD_ORDER_RTT_STORM_US", "order_rtt_storm_us"),
            ("HFT_STORMGUARD_BROKER_BACKPRESSURE_SILENCE_US", "broker_backpressure_silence_us"),
            ("HFT_STORMGUARD_BROKER_STALL_STORM_US", "broker_stall_storm_us"),
        ):
            raw = os.getenv(var)
            if not raw:
                continue
            try:
                setattr(self.thresholds, attr, int(raw))
            except ValueError:
                logger.warning(
                    "Invalid order-RTT threshold; keeping the built-in default",
                    var=var,
                    value=raw,
                    default_us=getattr(self.thresholds, attr),
                )

        for var, attr, cast in (
            ("HFT_STORMGUARD_ORDER_RTT_STORM_COUNT", "order_rtt_storm_count", int),
            ("HFT_STORMGUARD_ORDER_RTT_STORM_WINDOW_S", "order_rtt_storm_window_s", float),
            ("HFT_STORMGUARD_ORDER_RTT_STORM_DWELL_S", "order_rtt_storm_dwell_s", float),
        ):
            raw = os.getenv(var)
            if not raw:
                continue
            try:
                value = cast(raw)
            except ValueError:
                value = None
            if value is None or value < (1 if attr == "order_rtt_storm_count" else 0):
                logger.warning(
                    "Invalid order-RTT STORM setting; keeping the built-in default",
                    var=var,
                    value=raw,
                    default=getattr(self.thresholds, attr),
                )
                continue
            setattr(self.thresholds, attr, value)
        count = max(1, int(self.thresholds.order_rtt_storm_count))
        if self._order_rtt_slow_ts.maxlen != count:
            self._order_rtt_slow_ts = deque(self._order_rtt_slow_ts, maxlen=count)

        self._report_latency_input_health()

    def _order_rtt_armed(self) -> bool:
        return self.thresholds.order_rtt_warm_us > 0 or self.thresholds.order_rtt_storm_us > 0

    def _report_latency_input_health(self) -> None:
        """Log both latency inputs and flag a provably unreachable threshold.

        "Unreachable" is claimed only where it is provable: ``LoopStallWatchdog``
        force-exits the process at ``HFT_LOOP_STALL_KILL_S`` (default 60 s), so a
        loop-lag threshold beyond that is killed before it can fire. Thresholds
        merely far above the observed distribution are left to
        ``stormguard_latency_input_max_us`` rather than judged here.
        """
        try:
            stall_kill_seconds = float(os.getenv("HFT_LOOP_STALL_KILL_S", "60"))
        except ValueError:
            stall_kill_seconds = 60.0
        t = self.thresholds
        logger.info(
            "stormguard_latency_inputs",
            loop_lag_warm_us=t.latency_warm_us,
            loop_lag_storm_us=t.latency_storm_us,
            order_rtt_warm_us=t.order_rtt_warm_us,
            order_rtt_storm_us=t.order_rtt_storm_us,
            order_rtt_storm_count=t.order_rtt_storm_count,
            order_rtt_storm_window_s=t.order_rtt_storm_window_s,
            order_rtt_storm_dwell_s=t.order_rtt_storm_dwell_s,
            order_rtt_armed=self._order_rtt_armed(),
            broker_backpressure_silence_us=t.broker_backpressure_silence_us,
            broker_stall_storm_us=t.broker_stall_storm_us,
        )
        if stall_kill_seconds > 0:
            kill_us = int(stall_kill_seconds * 1_000_000)
            for name, value in (("warm", t.latency_warm_us), ("storm", t.latency_storm_us)):
                if value >= kill_us:
                    logger.warning(
                        "stormguard_latency_threshold_unreachable",
                        level=name,
                        threshold_us=value,
                        stall_kill_us=kill_us,
                        detail="loop-lag threshold is at or beyond the stall watchdog's force-exit deadline",
                    )
        self.publish_latency_input_health()

    def publish_latency_input_health(self) -> None:
        """Publish armed/unarmed per latency input, explicitly for both."""
        try:
            gauge = self.metrics.stormguard_latency_input_armed
            gauge.labels(input="loop_lag").set(
                1 if (self.thresholds.latency_warm_us > 0 or self.thresholds.latency_storm_us > 0) else 0
            )
            gauge.labels(input="order_rtt").set(1 if self._order_rtt_armed() else 0)
            max_gauge = self.metrics.stormguard_latency_input_max_us
            for name in ("loop_lag", "order_rtt"):
                max_gauge.labels(input=name).inc(0)
        except Exception:
            pass

    def observe_order_rtt_us(self, rtt_us: int) -> None:
        """Record one broker order round-trip for the next ``update()`` to read.

        Called from the order path, because that is where the measurement
        already exists: ``OrderAdapter`` times the broker SDK call and reports
        it as ``pipeline_latency_ns{stage="api_place_order"}``. Before this the
        ``order_rtt`` breaker input had no producer at all and sat at a
        structural zero, which reads exactly like a healthy input.

        It deliberately does **not** evaluate state. An escalation raised out of
        band is one ``update()`` cannot see, and an escalation ``update()``
        cannot see is not a latch -- that is precisely how a StormGuard HALT
        became 101 oscillations on 2026-08-21. The peak is drained by
        ``update()``, which stays the single evaluator.

        A plain int max: written from the event loop (the order path awaits the
        broker call on it) and drained there too, so no lock is needed, and an
        int assignment is atomic under the GIL regardless.
        """
        if rtt_us <= 0:
            return
        if rtt_us > self._order_rtt_peak_us:
            self._order_rtt_peak_us = rtt_us
        storm_us = self.thresholds.order_rtt_storm_us
        if storm_us > 0 and rtt_us >= storm_us:
            self._order_rtt_slow_ts.append(time.monotonic())
            self._order_rtt_slow_fresh = True

    def _order_rtt_storm_active(self) -> bool:
        """Whether the order-RTT STORM holds now. Called once per ``update()``.

        Starts on the tick whose slow orders bring the last
        ``order_rtt_storm_count`` of them within ``order_rtt_storm_window_s``;
        ends on the first tick with none new once ``order_rtt_storm_dwell_s``
        has passed since the last. With a count of 1 and a dwell of 0 this is
        the old single-sample rule.
        """
        fresh = self._order_rtt_slow_fresh
        self._order_rtt_slow_fresh = False
        slow = self._order_rtt_slow_ts
        t = self.thresholds
        if t.order_rtt_storm_us <= 0 or not slow:
            self._order_rtt_storm_on = False
            return False
        if not self._order_rtt_storm_on:
            if fresh and len(slow) == slow.maxlen and slow[-1] - slow[0] <= t.order_rtt_storm_window_s:
                self._order_rtt_storm_on = True
        elif not fresh and time.monotonic() - slow[-1] >= t.order_rtt_storm_dwell_s:
            self._order_rtt_storm_on = False
        return self._order_rtt_storm_on

    def _drain_order_rtt_peak_us(self) -> int:
        """Return the peak RTT since the last drain and reset it.

        Draining rather than decaying means one slow round-trip is seen by
        exactly one ``update()``: it cannot hold the breaker up across ticks
        after the broker has recovered.
        """
        peak = self._order_rtt_peak_us
        if peak:
            self._order_rtt_peak_us = 0
        return peak

    def _observe_latency_input(self, input_name: str, value_us: int) -> None:
        """Track the largest value an input has produced this process.

        Without it there is no way to see that a threshold sits 1000x above its
        own input — and no data to calibrate the order-RTT thresholds from.
        """
        if value_us <= 0:
            return
        try:
            gauge = self.metrics.stormguard_latency_input_max_us.labels(input=input_name)
            if value_us > gauge._value.get():
                gauge.set(value_us)
        except Exception:
            pass

    def _evaluate_target_state(
        self,
        latency_us: int,
        feed_gap_s: float,
        order_rtt_us: int = 0,
        order_rtt_storm: bool = False,
        broker_silence_us: int = 0,
    ) -> tuple[StormGuardState, str]:
        """Determine target state from health inputs. Priority: STORM > WARM > NORMAL.

        Never HALT: no health input is evidence that the book must stop, only
        that new risk should wait. HALT comes from ``trigger_halt`` (daily loss,
        kill switch, reconciliation, session lease), each with its own hold.

        ``latency_us`` is event-loop lag; ``order_rtt_us`` is broker round-trip.
        They are two quantities two orders of magnitude apart and each has its
        own threshold pair — one number cannot serve both.

        ``order_rtt_storm`` is the count-and-dwell decision
        (``_order_rtt_storm_active``); ``order_rtt_us`` alone reaches WARM.
        """
        t = self.thresholds
        if latency_us >= t.latency_storm_us:
            return StormGuardState.STORM, f"Latency {latency_us}us"
        if t.broker_stall_storm_us > 0 and broker_silence_us >= t.broker_stall_storm_us:
            return StormGuardState.STORM, f"Broker silent {broker_silence_us}us"
        if order_rtt_storm:
            return StormGuardState.STORM, (
                f"Order RTT {t.order_rtt_storm_count} orders >= {t.order_rtt_storm_us}us"
                f" within {t.order_rtt_storm_window_s:g}s, held {t.order_rtt_storm_dwell_s:g}s after the last"
            )
        if feed_gap_s >= t.feed_gap_storm_s and self._session_active:
            return StormGuardState.STORM, f"Feed Gap {feed_gap_s:.3f}s"
        norm_fail = self._norm_failure_active
        feat_fail = self._feature_failure_active
        if norm_fail or feat_fail:
            if norm_fail and feat_fail:
                return StormGuardState.STORM, "Component failure active (norm+feature)"
            if norm_fail:
                return StormGuardState.STORM, "Normalizer failure active"
            return StormGuardState.STORM, "FeatureEngine failure active"
        if latency_us >= t.latency_warm_us:
            return StormGuardState.WARM, "Latency Warning"
        if t.order_rtt_warm_us > 0 and order_rtt_us >= t.order_rtt_warm_us:
            return StormGuardState.WARM, f"Order RTT Warning {order_rtt_us}us"
        return StormGuardState.NORMAL, ""

    def update(
        self,
        *,
        latency_us: int = 0,
        feed_gap_s: float = 0.0,
        order_rtt_us: int = 0,
    ) -> StormGuardState:
        """
        Evaluate inputs and transition state.

        Args:
            latency_us: Event-loop lag in microseconds (platform health).
            feed_gap_s: Feed gap in seconds.
            order_rtt_us: Broker order round-trip in microseconds (trading-path
                health). Unarmed by default — see ``RiskThresholds``. Callers
                may leave this at 0: the peak recorded by
                ``observe_order_rtt_us`` since the last call is drained here and
                used when it is larger.
        """
        if order_rtt_us > 0:
            self.observe_order_rtt_us(order_rtt_us)
        peak_rtt_us = self._drain_order_rtt_peak_us()
        if peak_rtt_us > order_rtt_us:
            order_rtt_us = peak_rtt_us
        self._observe_latency_input("loop_lag", latency_us)
        self._observe_latency_input("order_rtt", order_rtt_us)
        broker_silence_us = self.broker_silence_ns() // 1000
        self._publish_broker_silence(broker_silence_us)
        new_state, reason = self._evaluate_target_state(
            latency_us, feed_gap_s, order_rtt_us, self._order_rtt_storm_active(), broker_silence_us
        )
        self._target_state_reason = reason

        fire_callback = False
        current_state: StormGuardState = self.state
        with self._state_lock:
            now = time.monotonic()
            if new_state > self.state:
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
                    pass
                else:
                    self._de_escalate_count = 0
                    if new_state >= StormGuardState.WARM and self.state < StormGuardState.WARM:
                        self._warm_entry_ts = now
                    if new_state >= StormGuardState.STORM and self.state < StormGuardState.STORM:
                        self._storm_entry_ts = now
                    if new_state == StormGuardState.HALT:
                        self._halt_entry_ts = now
                    _, fire_callback = self._transition(new_state, reason)
            elif new_state < self.state:
                if self.state == StormGuardState.HALT and self._reconciliation_hold:
                    self._de_escalate_count = 0
                    logger.warning(
                        "stormguard_deescalation_blocked_reconciliation_hold",
                        current_state=self.state.name,
                        target_state=new_state.name,
                    )
                    current_state = self.state
                    return current_state
                if self.state == StormGuardState.HALT and self._daily_loss_hold:
                    self._de_escalate_count = 0
                    logger.warning(
                        "stormguard_deescalation_blocked_daily_loss_hold",
                        current_state=self.state.name,
                        target_state=new_state.name,
                    )
                    current_state = self.state
                    return current_state
                if self.state == StormGuardState.HALT and self._kill_switch_hold:
                    self._de_escalate_count = 0
                    logger.warning(
                        "stormguard_deescalation_blocked_kill_switch_hold",
                        current_state=self.state.name,
                        target_state=new_state.name,
                    )
                    current_state = self.state
                    return current_state
                if self.state == StormGuardState.HALT and self._manual_latch_hold:
                    self._de_escalate_count = 0
                    logger.warning(
                        "stormguard_deescalation_blocked_manual_latch_hold",
                        current_state=self.state.name,
                        target_state=new_state.name,
                    )
                    current_state = self.state
                    return current_state
                if self.state == StormGuardState.HALT:
                    cooldown_s_for_log = self._halt_cooldown_s
                    cooldown_ok = (now - self._halt_entry_ts) >= self._halt_cooldown_s
                elif self.state >= StormGuardState.STORM:
                    cooldown_s_for_log = self._storm_cooldown_s
                    cooldown_ok = (now - self._storm_entry_ts) >= self._storm_cooldown_s
                elif self.state >= StormGuardState.WARM:
                    cooldown_s_for_log = self._warm_cooldown_s
                    cooldown_ok = self._warm_entry_ts == 0.0 or (now - self._warm_entry_ts) >= self._warm_cooldown_s
                else:
                    cooldown_s_for_log = 0.0
                    cooldown_ok = True

                if cooldown_ok:
                    self._de_escalate_count += 1
                    if self._de_escalate_count >= self._de_escalate_threshold:
                        old_for_log = self.state
                        self._de_escalate_count = 0
                        if old_for_log >= StormGuardState.STORM:
                            self._feed_gap_deescalation_ts = now
                            self._latency_deescalation_ts = now
                        if old_for_log == StormGuardState.WARM and new_state < StormGuardState.WARM:
                            self._warm_deescalation_ts = now
                        self._storm_entry_ts = 0.0
                        self._warm_entry_ts = 0.0
                        _, fire_callback = self._transition(new_state, "Recovery")
                        logger.info(
                            "StormGuard de-escalated after hysteresis",
                            from_state=old_for_log.name,
                            to_state=new_state.name,
                            cooldown_s=cooldown_s_for_log,
                            threshold=self._de_escalate_threshold,
                        )
                else:
                    self._de_escalate_count = 0
            else:
                if new_state >= StormGuardState.STORM:
                    self._de_escalate_count = 0

            current_state = self.state

        self._emit_pending_transition()

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
            symbol: Instrument driving this LOB update. Passed into the
                detector, which holds one instrument's return window — a
                changed symbol resets it rather than mixing price series.

        Returns:
            Current StormGuardState after potential escalation.
        """
        if spread_scaled < 0:
            with self._state_lock:
                if self.state < StormGuardState.STORM:
                    self._storm_entry_ts = time.monotonic()
                    self._de_escalate_count = 0
                    self._transition(
                        StormGuardState.STORM,
                        f"Crossed book: spread_scaled={spread_scaled}",
                    )
            self._emit_pending_transition()
            return self.state
        if mid_price_x2 <= 0:
            return self.state

        detector = self._drift_burst_detector
        if detector is None:
            return self.state

        result = detector.evaluate(mid_price_x2, spread_scaled, imbalance, ts, symbol)

        try:
            _sym = symbol or "unknown"
            self.metrics.stormguard_toxicity_score.labels(symbol=self.metrics.cap_symbol(_sym)).set(
                result.toxicity_score
            )
            if result.burst_detected:
                _tox_type = getattr(result.burst_event, "toxicity_type", "") or "unknown"
                self.metrics.drift_burst_detected_total.labels(
                    symbol=self.metrics.cap_symbol(_sym), toxicity_type=_tox_type
                ).inc()
        except Exception:
            pass

        target: StormGuardState | None
        reason = ""
        if result.toxicity_score > self._warm_toxicity_entry:
            target = StormGuardState.WARM
            reason = f"DriftBurst WARM: toxicity={result.toxicity_score:.3f}"
        elif self.state == StormGuardState.WARM and result.toxicity_score > self._warm_toxicity_exit:
            target = None
        else:
            return self.state

        fire_callback = False
        with self._state_lock:
            now = time.monotonic()
            if target is None:
                if self.state == StormGuardState.WARM:
                    self._warm_entry_ts = now
                current_state = self.state
                self._emit_pending_transition()
                return current_state
            if (
                target == StormGuardState.WARM
                and self.state < StormGuardState.WARM
                and self._warm_deescalation_ts > 0
                and (now - self._warm_deescalation_ts) < self._warm_reescalation_cooldown_s
            ):
                return self.state
            if target > self.state:
                self._de_escalate_count = 0
                self._warm_entry_ts = now
                _, fire_callback = self._transition(target, reason)
                logger.info(
                    "StormGuard drift_burst escalation",
                    symbol=symbol,
                    new_state=target.name,
                    toxicity_score=f"{result.toxicity_score:.3f}",
                    burst_detected=result.burst_detected,
                )
            elif target == StormGuardState.WARM and self.state == StormGuardState.WARM:
                self._warm_entry_ts = now

            current_state = self.state

        self._emit_pending_transition()

        if fire_callback:
            self._fire_halt_callback()

        return current_state

    def _transition(self, new_state: StormGuardState, reason: str) -> tuple[StormGuardState, bool]:
        """Transition state machine. Returns (old_state, should_fire_halt_callback).

        IMPORTANT (lock ordering):
        * Called by methods that hold ``_state_lock``.
        * This function MUST NOT call into Prometheus metric internals or the
          audit singleton while the lock is held — those acquire their own
          locks (prometheus_client MutexValue, audit asyncio.Queue) and the
          reverse ordering (scraper thread reads the same metric collector)
          creates a cross-thread deadlock risk.
        * Side-effects (metric emission + audit row) are recorded on the
          instance in ``_pending_transition_emit`` and flushed by
          ``_emit_pending_transition`` AFTER the caller releases
          ``_state_lock``. Callers MUST invoke
          ``_emit_pending_transition()`` immediately after exiting the
          ``with self._state_lock:`` block (P1 fix).
        """
        old_state = self.state
        self.state = new_state
        self.last_state_change = time.monotonic()

        logger.warning("StormGuard Transition", old=old_state.name, new=new_state.name, reason=reason)

        self._pending_transition_emit.append(
            {
                "old_state_int": int(old_state),
                "new_state_int": int(new_state),
                "old_name": old_state.name,
                "new_name": new_state.name,
                "reason": reason,
            }
        )

        should_fire = (
            new_state == StormGuardState.HALT
            and old_state != StormGuardState.HALT
            and self._on_halt_callback is not None
        )
        return old_state, should_fire

    def _emit_pending_transition(self) -> None:
        """Flush queued transition side-effects (metrics + audit) outside _state_lock.

        P1 fix: avoids nested-lock ordering (_state_lock → prometheus metric lock)
        that could deadlock against the Prometheus scraper thread.

        Called by every path that invokes ``_transition`` after releasing the
        state lock. Safe to call when no pending entries exist (no-op).
        """
        if not self._pending_transition_emit:
            return
        pending = self._pending_transition_emit
        self._pending_transition_emit = []

        for entry in pending:
            new_state_int = entry["new_state_int"]
            old_state_int = entry["old_state_int"]
            direction = "escalation" if new_state_int > old_state_int else "de_escalation"

            try:
                self.metrics.stormguard_mode.labels(strategy="system").set(new_state_int)
            except Exception as exc:
                logger.warning("stormguard_metric_update_failed", metric="mode", error=str(exc))

            if StormGuardState.HALT in (new_state_int, old_state_int):
                try:
                    gauge = self.metrics.stormguard_halt_reason
                    with self._halt_label_lock:
                        if self._halt_reason_label:
                            gauge.labels(reason=self._halt_reason_label).set(0)
                            self._halt_reason_label = ""
                        if new_state_int == StormGuardState.HALT:
                            self._halt_reason_label = halt_reason_label(entry["reason"])
                            gauge.labels(reason=self._halt_reason_label).set(1)
                except Exception as exc:
                    logger.warning("stormguard_metric_update_failed", metric="halt_reason", error=str(exc))

            try:
                self.metrics.stormguard_transitions_total.labels(direction=direction).inc()
            except Exception as exc:
                logger.warning("stormguard_metric_update_failed", metric="transitions", error=str(exc))

            if direction == "escalation":
                try:
                    self.metrics.stormguard_escalations_total.labels(to_state=StormGuardState(new_state_int).name).inc()
                except Exception as exc:
                    logger.warning("stormguard_metric_update_failed", metric="escalations", error=str(exc))

            try:
                from hft_platform.recorder.audit import get_audit_writer

                audit = get_audit_writer()
                audit.log_guardrail_transition(
                    {
                        "old_state": entry["old_name"],
                        "new_state": entry["new_name"],
                        "reason": entry["reason"],
                    }
                )
            except Exception as exc:
                logger.warning("audit_guardrail_transition_failed", error=str(exc))

    def _fire_halt_callback(self) -> None:
        """Invoke the on_halt_callback. Must be called OUTSIDE _state_lock.

        P0-I4: when a coroutine halt-callback is triggered from a non-asyncio
        thread (e.g. bootstrap lease-refresh daemon), ``asyncio.get_running_loop()``
        raises RuntimeError. Use the engine loop reference bound via ``bind_loop()``
        so coroutines can be scheduled thread-safely via
        ``asyncio.run_coroutine_threadsafe``.
        """
        if self._on_halt_callback is None:
            return
        try:
            result = self._on_halt_callback()
        except Exception as exc:
            logger.error(
                "on_halt_callback_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        if not asyncio.iscoroutine(result):
            return

        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                result.close()
                logger.warning("halt_callback_coroutine_no_loop")
                return

        try:
            future = asyncio.run_coroutine_threadsafe(result, loop)
            future.add_done_callback(self._halt_callback_done)
        except RuntimeError as exc:
            result.close()
            logger.warning("halt_callback_schedule_failed", error=str(exc))

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
                self._target_state_reason = reason
                self._transition(StormGuardState.STORM, reason)

        self._emit_pending_transition()

    def trigger_halt(self, reason: str) -> None:
        """Manual or Supervisor override to force HALT.

        Thread-safe: callable from any thread (engine loop, supervisor, daemon
        lease-refresh thread, broker callback thread). The coroutine halt-callback
        is scheduled via ``_fire_halt_callback`` which uses ``bind_loop()``'s
        engine-loop reference for cross-thread dispatch (P0-I4).

        H1: marks every in-flight dispatch ticket as ``halted=True`` under
        ``_state_lock``. Callers blocked in ``await broker.place_order()`` will
        observe ``halted=True`` when they call ``end_dispatch`` and can emit a
        defensive cancel for the order that completed during the HALT window.
        """
        fire_callback = False
        with self._state_lock:
            now = time.monotonic()
            if self.state < StormGuardState.STORM:
                self._storm_entry_ts = now
            self._halt_entry_ts = now
            self._de_escalate_count = 0
            _, fire_callback = self._transition(StormGuardState.HALT, reason)
            self._halt_reason = reason
            for ticket in self._inflight_dispatch_tickets.values():
                ticket["halted"] = True

        self._emit_pending_transition()

        if fire_callback:
            self._fire_halt_callback()

    def validate(self, intent: OrderIntent) -> tuple[bool, str]:
        """RiskEngine's StormGuard gate: the state policy, then broker back-pressure.

        Back-pressure is checked here and not in ``_validate_locked`` because
        that body also opens dispatch tickets (``begin_dispatch``) for orders
        already on their way to the broker; a ticket refused there is not a
        refused order, only a lost HALT-TOCTOU cancel. ``OrderAdapter`` asks
        ``backpressure_blocks`` itself just before dispatch.
        """
        with self._state_lock:
            ok, reason = self._validate_locked(intent)
        if ok and self.backpressure_blocks(intent):
            return False, "BROKER_BACKPRESSURE"
        return ok, reason

    def set_broker_stall_provider(self, provider: Callable[[], int] | None) -> None:
        """Wire ``() -> int`` nanoseconds of broker order-path silence.

        ``OrderAdapter.broker_silence_ns`` in production. Bound after
        construction, like ``set_position_provider``: StormGuard is built
        before the adapter.
        """
        self._broker_stall_provider = provider

    def broker_silence_ns(self) -> int:
        """Current broker order-path silence; 0 when unwired or the provider fails."""
        provider = self._broker_stall_provider
        if provider is None:
            return 0
        try:
            return max(0, int(provider()))
        except Exception:
            return 0

    def backpressure_blocks(self, intent: OrderIntent) -> bool:
        """True iff ``intent`` must wait for the broker to answer before it may go.

        Same exemptions as STORM: only NEW and AMEND are held, and not when
        they reduce the position or the strategy is HALT-exempt. Stateless:
        it reads the silence now, so the first answer releases it.
        """
        threshold_us = self.thresholds.broker_backpressure_silence_us
        if threshold_us <= 0:
            return False
        if intent.intent_type not in (IntentType.NEW, IntentType.AMEND):
            return False
        if self.broker_silence_ns() < threshold_us * 1000:
            return False
        if intent.strategy_id in self._halt_exempt_strategies:
            return False
        if self._intent_reduces_position(intent):
            return False
        return True

    def _publish_broker_silence(self, silence_us: int) -> None:
        try:
            self.metrics.order_broker_silence_seconds.set(silence_us / 1_000_000)
        except Exception:
            logger.debug("broker_silence_publish_failed", exc_info=True)

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

    def begin_dispatch(self, intent: OrderIntent) -> tuple[bool, str, int | None]:
        """H1: atomic HALT validation + ticket allocation for async dispatches.

        Returns ``(ok, reason, ticket_id)``. When ``ok`` is True the caller
        has been issued an in-flight dispatch ticket which must be paired
        with a single ``end_dispatch(ticket_id)`` call regardless of how
        the dispatch terminates (success, exception, timeout). When ``ok``
        is False ``ticket_id`` is None and no cleanup is required.

        Unlike ``check_and_submit`` this is safe to use around an ``await``
        on the broker SDK because the lock is **not** held across the
        call; instead a concurrent ``trigger_halt`` flips the ticket's
        ``halted`` flag (under ``_state_lock``), which the caller observes
        in ``end_dispatch`` and uses to drive a defensive cancel — even if
        the broker accepted the order during the HALT window.
        """
        with self._state_lock:
            ok, reason = self._validate_locked(intent)
            if not ok:
                return False, reason, None
            ticket_id = self._next_ticket_id
            self._next_ticket_id += 1
            self._inflight_dispatch_tickets[ticket_id] = {"halted": False}
            return True, reason, ticket_id

    def end_dispatch(self, ticket_id: int) -> bool:
        """H1: drop the ticket allocated by ``begin_dispatch`` and report
        whether HALT was triggered while it was outstanding.

        Returns True iff a HALT transition occurred between
        ``begin_dispatch`` and ``end_dispatch`` for this ticket. The caller
        should treat True as "broker may have accepted the order during
        the HALT window — emit a defensive cancel". False means either the
        dispatch completed before any HALT, or the ticket id is unknown
        (idempotent cleanup on shutdown / unit-test paths).
        """
        with self._state_lock:
            ticket = self._inflight_dispatch_tickets.pop(ticket_id, None)
        if ticket is None:
            return False
        return bool(ticket.get("halted", False))

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
        except Exception:
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
        self._emit_pending_transition()
        try:
            self.metrics.feature_engine_escalation_total.inc()
        except Exception:
            pass
        logger.warning(
            "stormguard_feature_failure_escalation",
            consecutive_failures=count,
        )

    _FEATURE_RECOVERY_HOLD_S: float = float(os.getenv("HFT_STORMGUARD_FEATURE_RECOVERY_HOLD_S", "5"))

    def report_feature_recovery(self) -> None:
        """Clear feature-failure flag after FeatureEngine recovers.

        Does NOT transition state directly — the next ``update()`` cycle will
        re-evaluate all conditions (latency, order RTT, feed gap) and
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
            elapsed = time.monotonic() - self._feature_failure_storm_ts
            if elapsed < self._FEATURE_RECOVERY_HOLD_S:
                logger.debug(
                    "stormguard_feature_recovery_suppressed",
                    elapsed_s=round(elapsed, 2),
                    hold_s=self._FEATURE_RECOVERY_HOLD_S,
                )
                return
            self._feature_failure_active = False
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
        self._emit_pending_transition()
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

    @property
    def state_reason(self) -> str:
        """Why the guard is in its current state (read-only).

        ``StormGuard Transition`` carries this at the moment of the change and
        never again, so anything logging a *sustained* state -- the HALT status
        line in ``services/system.py`` -- had no way to say what it was holding
        for. Reads the live value, which is the one the gate acts on: on
        2026-09-08 a HALT logged for four hours as ``Drawdown -233bps`` while
        the exported drawdown gauge read something else entirely.

        In HALT the entry reason alone can mislead: a HALT entered for one cause
        can later be held by another (the daily hard limit tripping during a
        queue-drop HALT never calls ``trigger_halt`` again), so the active holds
        are named too.
        """
        if self.state != StormGuardState.HALT:
            return self._target_state_reason
        holds = [
            name
            for name, held in (
                ("kill_switch", self._kill_switch_hold),
                ("manual_latch", self._manual_latch_hold),
                ("reconciliation", self._reconciliation_hold),
                ("daily_loss", self._daily_loss_hold),
            )
            if held
        ]
        reason = self._halt_reason or self._target_state_reason
        if holds:
            return f"{reason} (held by {'+'.join(holds)})"
        return reason

    def set_daily_loss_hold(self, hold: bool) -> None:
        """Set or clear the daily-loss hold on HALT de-escalation.

        When ``hold=True``, HALT will not auto-recover via ``update()``. The
        risk engine keeps this in sync with
        ``DailyLossLimitValidator.halt_triggered``, which clears only at the
        05:00 daily reset or an operator force-reset.
        Thread-safe.
        """
        with self._state_lock:
            old = self._daily_loss_hold
            self._daily_loss_hold = hold
        if old != hold:
            logger.warning(
                "stormguard_daily_loss_hold_changed",
                hold=hold,
                current_state=self.state.name,
            )

    @property
    def daily_loss_hold(self) -> bool:
        """Whether the daily-loss hold is active (read-only)."""
        return self._daily_loss_hold

    def set_kill_switch_hold(self, hold: bool) -> None:
        """Set or clear the kill-switch hold on HALT de-escalation.

        ``HFTSystem`` keeps this in sync with the latch file on every supervise
        tick, so removing the file (``hft risk resume``) releases the hold and
        HALT de-escalates through the normal cooldown. A startup recovery halt
        additionally pins it for the process lifetime: that process holds an
        empty position store and has its checkpoint writer suppressed, so it
        must not resume without a restart no matter what happens to the file.
        Thread-safe.
        """
        with self._state_lock:
            old = self._kill_switch_hold
            self._kill_switch_hold = hold
        try:
            self.metrics.kill_switch_active.set(1 if hold else 0)
        except Exception as exc:
            logger.warning("kill_switch_gauge_failed", error=str(exc))
        if old != hold:
            logger.warning(
                "stormguard_kill_switch_hold_changed",
                hold=hold,
                current_state=self.state.name,
            )

    @property
    def kill_switch_hold(self) -> bool:
        """Whether the kill-switch hold is active (read-only)."""
        return self._kill_switch_hold

    def set_manual_latch_hold(self, hold: bool) -> None:
        """Set or clear the manual-latch hold on HALT de-escalation.

        ``HFTSystem`` sets this every supervise tick from the presence of the
        files under ``manual_latches.latch_dir()``. Removing them
        (``hft risk rearm``) releases the hold and HALT de-escalates through the
        normal cooldown. Thread-safe.
        """
        with self._state_lock:
            old = self._manual_latch_hold
            self._manual_latch_hold = hold
        if old != hold:
            logger.warning(
                "stormguard_manual_latch_hold_changed",
                hold=hold,
                current_state=self.state.name,
            )

    @property
    def manual_latch_hold(self) -> bool:
        """Whether the manual-latch hold is active (read-only)."""
        return self._manual_latch_hold
