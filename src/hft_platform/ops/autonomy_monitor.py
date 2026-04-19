"""AutonomyMonitor — async polling reactor for autonomous trading health signals.

Reads StormGuard state, broker connectivity, infra health, and reconciliation
drift, then triggers proportional responses (flatten, reduce-only, notify) via
existing actuators.  Never evaluates P&L directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.core import timebase
from hft_platform.ops.flatten_gate import FlattenGate
from hft_platform.ops.margin_monitor import MarginMonitor

logger = structlog.get_logger("autonomy_monitor")


@dataclass(slots=True)
class MonitorDecision:
    """A single autonomy decision emitted by the monitor."""

    rule_name: str
    action: str  # "enter_reduce_only", "flatten_all", "notify_quarantine"
    reason: str
    scope: str  # "platform", "strategy"
    rearm: str  # "auto", "manual"


class AutonomyMonitor:
    """Async health-signal reactor for the autonomous trading system.

    Polls every *interval_s* seconds, reads health signals from injected
    collaborators, and triggers proportional responses.  It is a **reactor**:
    it reacts to ``StormGuard.state == HALT`` (set by ``RiskEngine``), never
    evaluates P&L directly.
    """

    __slots__ = (
        "_storm_guard",
        "_platform_degrade",
        "_platform_inputs",
        "_recon_service",
        "_evidence_writer",
        "_position_flattener",
        "_broker_client",
        "_notification_dispatcher",
        "_flatten_gate",
        "_margin_monitor",
        "_interval_s",
        "_heartbeat_interval_s",
        "_running",
        "_task",
        "_cooldowns",
        "_cooldown_durations",
        "_broker_disconnect_since_ns",
        "_broker_was_connected",
        "_halt_reacted",
        "_halt_reacted_ns",
        "_halt_flatten_attempts",
        "_halt_max_retries",
        "_halt_next_retry_ns",
        "_last_heartbeat_ns",
    )

    def __init__(
        self,
        storm_guard: Any,
        platform_degrade: Any,
        platform_inputs: Any,
        recon_service: Any | None = None,
        evidence_writer: Any | None = None,
        position_flattener: Any | None = None,
        broker_client: Any | None = None,
        notification_dispatcher: Any | None = None,
        flatten_gate: FlattenGate | None = None,
        margin_monitor: MarginMonitor | None = None,
        interval_s: float = 5.0,
        heartbeat_interval_s: float = 1800.0,
    ) -> None:
        self._storm_guard = storm_guard
        self._platform_degrade = platform_degrade
        self._platform_inputs = platform_inputs
        self._recon_service = recon_service
        self._evidence_writer = evidence_writer
        self._position_flattener = position_flattener
        self._broker_client = broker_client
        self._notification_dispatcher = notification_dispatcher
        self._flatten_gate = flatten_gate
        self._margin_monitor = margin_monitor
        self._interval_s = interval_s
        self._heartbeat_interval_s = heartbeat_interval_s
        self._running: bool = False
        self._task: asyncio.Task[None] | None = None

        # Cooldown tracking: rule_name -> last_triggered_ns
        self._cooldowns: dict[str, int] = {}
        self._cooldown_durations: dict[str, int] = {
            "halt_reaction": 0,
            "broker_disconnect": 120_000_000_000,  # 120s in ns
            "feed_gap_majority": 120_000_000_000,
            "reconnect_flapping": 120_000_000_000,
            "reconciliation_drift": 120_000_000_000,
            "persistence_failure": 120_000_000_000,
            "rss_unhealthy": 120_000_000_000,
        }

        # Broker disconnect tracking
        self._broker_disconnect_since_ns: int = 0
        self._broker_was_connected: bool = True

        # HALT reaction tracking (only react once per HALT episode)
        self._halt_reacted: bool = False
        self._halt_reacted_ns: int = 0
        self._halt_flatten_attempts: int = 0
        self._halt_max_retries: int = 3
        self._halt_next_retry_ns: int = 0

        # Heartbeat tracking
        self._last_heartbeat_ns: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the monitor loop directly (used by the supervisor task manager)."""
        self._running = True
        logger.info("autonomy_monitor_started", interval_s=self._interval_s)
        await self._monitor_loop()

    async def start(self) -> None:
        """Start the async monitor loop (legacy: creates its own internal task)."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("autonomy_monitor_started", interval_s=self._interval_s)

    async def stop(self) -> None:
        """Stop the monitor loop gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("autonomy_monitor_stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                decisions = self._evaluate()
                if decisions:
                    await self._execute(decisions)
                    self._apply_cooldowns(decisions)
                await self._maybe_heartbeat()
                if self._flatten_gate is not None and self._position_flattener is not None:
                    await _handle_flatten_request(self._flatten_gate, self._position_flattener)
                await self._check_margin()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("autonomy_monitor_error", error=str(exc))
            await asyncio.sleep(self._interval_s)

    # ------------------------------------------------------------------
    # Margin monitoring
    # ------------------------------------------------------------------

    async def _check_margin(self) -> None:
        """Poll margin monitor and act on threshold breaches."""
        if self._margin_monitor is None:
            return

        now_ns = timebase.now_ns()
        result = await self._margin_monitor.check(now_ns)
        if result is None:
            return

        if result.action == "critical":
            self._platform_degrade.enter_reduce_only(
                reason=f"margin_critical_{result.ratio:.0%}",
            )
            if self._notification_dispatcher:
                await self._notification_dispatcher.notify_margin_critical(
                    ratio=result.ratio,
                    used=result.margin_used,
                    available=result.margin_available,
                )
        elif result.action == "warn":
            if self._notification_dispatcher:
                await self._notification_dispatcher.notify_margin_warning(
                    ratio=result.ratio,
                    used=result.margin_used,
                    available=result.margin_available,
                )

    # ------------------------------------------------------------------
    # Evaluation helpers
    # ------------------------------------------------------------------

    def _check_broker_disconnect(self, decisions: list[MonitorDecision], now_ns: int) -> None:
        if self._broker_client is None:
            return
        connected = False
        try:
            connected = self._broker_client.is_connected()
        except Exception:
            pass

        if not connected:
            if self._broker_was_connected:
                self._broker_disconnect_since_ns = now_ns
                self._broker_was_connected = False

            elapsed_ns = now_ns - self._broker_disconnect_since_ns
            if elapsed_ns > 300_000_000_000 and not self._is_on_cooldown("broker_disconnect", now_ns):
                decisions.append(
                    MonitorDecision(
                        rule_name="broker_disconnect",
                        action="enter_reduce_only",
                        reason="broker_unavailable",
                        scope="platform",
                        rearm="auto",
                    )
                )
        else:
            self._broker_was_connected = True
            self._broker_disconnect_since_ns = 0

    # ------------------------------------------------------------------
    # Evaluation (pure function of current state)
    # ------------------------------------------------------------------

    def _evaluate(self) -> list[MonitorDecision]:
        """Read all signals and return decisions."""
        decisions: list[MonitorDecision] = []
        now_ns = timebase.now_ns()

        # 1. HALT reaction (highest priority)
        if (
            self._storm_guard.state == StormGuardState.HALT
            and not self._halt_reacted
            and self._halt_flatten_attempts < self._halt_max_retries
            and now_ns >= self._halt_next_retry_ns
        ):
            decisions.append(
                MonitorDecision(
                    rule_name="halt_reaction",
                    action="flatten_all",
                    reason="stormguard_halt",
                    scope="platform",
                    rearm="manual",
                )
            )
            return decisions  # HALT is exclusive -- don't stack other decisions

        # Reset halt_reacted when StormGuard leaves HALT (with cooldown)
        if self._storm_guard.state != StormGuardState.HALT:
            # Only reset after cooldown to prevent double-flatten on oscillation
            if self._halt_reacted and (now_ns - self._halt_reacted_ns) >= 60_000_000_000:
                self._halt_reacted = False
                self._halt_flatten_attempts = 0
                self._halt_next_retry_ns = 0

        # When already in reduce-only, skip enter_reduce_only decisions
        # but still check broker connectivity for escalation/diagnostic
        if self._platform_degrade.reduce_only_active:
            self._check_broker_disconnect(decisions, now_ns)
            return decisions

        # 2. Broker disconnect > 5 min
        self._check_broker_disconnect(decisions, now_ns)

        # 3. Infra health from PlatformDegradeInputs
        try:
            reasons = self._platform_inputs.reduce_only_reasons()
        except Exception:
            reasons = []

        # DATA_LOSS (CH + WAL both failed) → HALT, not just reduce-only
        if "recorder_data_loss" in reasons:
            if not self._is_on_cooldown("recorder_data_loss", now_ns):
                self._storm_guard.trigger_halt("recorder_data_loss")
                decisions.append(
                    MonitorDecision(
                        rule_name="recorder_data_loss",
                        action="trigger_halt",
                        reason="recorder_data_loss",
                        scope="platform",
                        rearm="manual",
                    )
                )
                return decisions

        _INFRA_REASON_MAP: dict[str, str] = {
            "rss_unhealthy": "rss_unhealthy",
            "wal_backlog_unhealthy": "persistence_failure",
            "clickhouse_unhealthy": "clickhouse_unhealthy",
            "redis_unhealthy": "redis_unhealthy",
            "feed_reconnect_unhealthy": "feed_gap_majority",
            "feed_reconnect_pending": "feed_gap_majority",
            "feed_reconnect_flapping": "feed_gap_majority",
            "queue_depth_exceeded": "queue_depth_exceeded",
        }
        for reason in reasons:
            rule_name = _INFRA_REASON_MAP.get(reason)
            if rule_name is not None:
                if not self._is_on_cooldown(rule_name, now_ns):
                    decisions.append(
                        MonitorDecision(
                            rule_name=rule_name,
                            action="enter_reduce_only",
                            reason=reason,
                            scope="platform",
                            rearm="manual",
                        )
                    )
                    break  # one infra reason is enough

        # 4. Reconciliation drift
        if self._recon_service is not None:
            try:
                drift = self._recon_service.drift_streak
            except Exception:
                drift = 0
            if drift >= 2 and not self._is_on_cooldown("reconciliation_drift", now_ns):
                decisions.append(
                    MonitorDecision(
                        rule_name="reconciliation_drift",
                        action="enter_reduce_only",
                        reason="reconciliation_drift",
                        scope="platform",
                        rearm="manual",
                    )
                )

        return decisions

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute(self, decisions: list[MonitorDecision]) -> None:
        for decision in decisions:
            logger.warning(
                "autonomy_decision",
                rule=decision.rule_name,
                action=decision.action,
                reason=decision.reason,
            )

            if decision.action == "flatten_all" and self._position_flattener:
                self._halt_flatten_attempts += 1
                try:
                    result = await self._position_flattener.flatten_all()
                    self._halt_reacted = True
                    self._halt_reacted_ns = timebase.now_ns()
                    self._halt_next_retry_ns = 0
                    if self._notification_dispatcher:
                        await self._notification_dispatcher.notify_flatten_result(
                            scope="all",
                            fully_closed=result.fully_closed,
                            partially_closed=result.partially_closed,
                            failed=result.failed,
                            failed_symbols=result.failed_symbols,
                        )
                except Exception as exc:
                    backoff_ns = int(self._interval_s * 1_000_000_000 * (2 ** (self._halt_flatten_attempts - 1)))
                    self._halt_next_retry_ns = timebase.now_ns() + backoff_ns
                    logger.error(
                        "flatten_all_failed",
                        error=str(exc),
                        attempt=self._halt_flatten_attempts,
                        max_retries=self._halt_max_retries,
                    )
                    if self._halt_flatten_attempts >= self._halt_max_retries:
                        logger.critical(
                            "flatten_all_max_retries_exhausted",
                            attempts=self._halt_flatten_attempts,
                        )
                        self._halt_reacted = True  # stop retrying
                        self._halt_reacted_ns = timebase.now_ns()
                        if self._notification_dispatcher:
                            try:
                                await self._notification_dispatcher.notify_flatten_result(
                                    scope="all",
                                    fully_closed=0,
                                    partially_closed=0,
                                    failed=-1,
                                    failed_symbols=["FLATTEN_EXHAUSTED"],
                                )
                            except Exception:  # noqa: BLE001
                                logger.debug("notify_flatten_exhausted_failed")

            elif decision.action == "enter_reduce_only":
                try:
                    self._platform_degrade.enter_reduce_only(reason=decision.reason)
                except Exception as exc:
                    logger.error("enter_reduce_only_failed", error=str(exc))

            # Record evidence
            if self._evidence_writer:
                try:
                    self._evidence_writer.record_transition(
                        scope=decision.scope,
                        mode=decision.action,
                        reason=decision.reason,
                        manual_rearm_required=(decision.rearm == "manual"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("evidence_write_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Cooldown helpers
    # ------------------------------------------------------------------

    def _apply_cooldowns(self, decisions: list[MonitorDecision]) -> None:
        now_ns = timebase.now_ns()
        for d in decisions:
            self._cooldowns[d.rule_name] = now_ns

    def _is_on_cooldown(self, rule_name: str, now_ns: int) -> bool:
        last = self._cooldowns.get(rule_name, 0)
        if last == 0:
            return False
        duration = self._cooldown_durations.get(rule_name, 120_000_000_000)
        return (now_ns - last) < duration

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _maybe_heartbeat(self) -> None:
        if self._notification_dispatcher is None:
            return
        now_ns = timebase.now_ns()
        if (now_ns - self._last_heartbeat_ns) < int(self._heartbeat_interval_s * 1_000_000_000):
            return
        self._last_heartbeat_ns = now_ns
        try:
            pnl = 0
            if hasattr(self._storm_guard, "position_store"):
                pnl = getattr(self._storm_guard.position_store, "total_pnl", 0)
            await self._notification_dispatcher.notify_heartbeat(
                autonomy_state=self._storm_guard.state.name,
                pnl_scaled=pnl,
                strategies_active=0,  # placeholder
                feed_status="ok" if self._broker_was_connected else "disconnected",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("heartbeat_notification_failed", error=str(exc))


async def _handle_flatten_request(gate: FlattenGate, flattener: Any) -> None:
    """Poll FlattenGate and execute if a PENDING request exists.

    Called from AutonomyMonitor._monitor_loop each iteration.
    Claims the request, dispatches to the appropriate flattener method,
    and writes back the result via gate.complete() or gate.fail().
    """
    req = gate.claim()
    if req is None:
        return

    try:
        if req.scope == "track" and req.scope_id:
            result = await flattener.flatten_track(req.scope_id, [])
        elif req.scope == "strategy" and req.scope_id:
            result = await flattener.flatten_strategy(req.scope_id)
        else:
            result = await flattener.flatten_all()

        gate.complete(
            fully_closed=result.fully_closed,
            partially_closed=result.partially_closed,
            failed=result.failed,
            failed_symbols=result.failed_symbols,
        )
        logger.info(
            "flatten_gate_executed",
            scope=req.scope,
            fully_closed=result.fully_closed,
            failed=result.failed,
        )
    except Exception as exc:
        gate.fail(str(exc))
        logger.error("flatten_gate_error", scope=req.scope, error=str(exc))
