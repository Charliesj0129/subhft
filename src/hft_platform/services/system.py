import asyncio
import collections
import gc
import os
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Dict, Optional

from structlog import get_logger

from hft_platform.contracts.strategy import (
    IntentType,
    RiskFeedback,
    Side,
    typed_intent_id,
    typed_intent_side,
    typed_intent_strategy_id,
    typed_intent_symbol,
)
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.core.session_hooks import SessionHookManager
from hft_platform.observability.health import HealthServer
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops import quarantine_requests, rearm_requests
from hft_platform.ops.evidence import get_shared_autonomy_evidence_writer
from hft_platform.ops.flatten_gate import FlattenGate, FlattenStatus
from hft_platform.ops.manual_rearm import ManualRearmGate
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.risk import daily_loss_state, kill_switch, manual_latches
from hft_platform.risk.expiry import DEFAULT_EXPIRY_CUTOFF_MINUTE
from hft_platform.risk.loss_exit import TRIGGER_KILL_SWITCH, LossExitController, OpenPosition, is_platform_exit
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.risk.validators import DailyLossLimitValidator, ExpiringContractValidator
from hft_platform.services.bootstrap import SystemBootstrapper, resolve_order_mode
from hft_platform.services.heartbeat import DEFAULT_HEARTBEAT_PATH, heartbeat_writable, write_heartbeat
from hft_platform.services.loop_watchdog import LoopStallWatchdog
from hft_platform.utils.logging import configure_logging

logger = get_logger("system")


def _payload_field(payload: Any, key: str) -> Any:
    """Read a field from a broker payload that may be a dict or an object.

    shioaji 1.5.x hands the order topic a Rust-backed mapping while other
    callbacks arrive as plain objects, so every payload read has to work both
    ways.
    """
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload.get(key)
    return getattr(payload, key, None)


def _read_kill_switch_reason(path: str) -> str:
    """Read kill-switch reason from JSON file. Runs in executor thread."""
    return kill_switch.read_reason(path)


_KILL_FLATTEN_ACTORS = frozenset({"cli", "telegram"})


def _read_kill_switch_actor(path: str) -> str:
    """Actor recorded in the kill-switch file, ``"unreadable"`` when it cannot be read. Runs in an executor."""
    try:
        return str(kill_switch.read_payload(path).get("actor", ""))
    except (OSError, ValueError, AttributeError):
        return "unreadable"


def _log_safety_dispatch_error(task: "asyncio.Task[None]") -> None:
    """done_callback for safety-order dispatch tasks during HALT drain."""
    exc = task.exception() if not task.cancelled() else None
    if exc is not None:
        logger.critical("halt_drain_safety_cmd_execute_failed", error=str(exc))


def _audit_persistence_writer_for_recorder(recorder: Any) -> Any | None:
    """Return the correct AuditWriter sink for the recorder's active mode."""
    mode = getattr(getattr(recorder, "_mode", ""), "value", getattr(recorder, "_mode", ""))
    if mode == "wal_first":
        queue = getattr(recorder, "queue", None)
        if queue is None:
            logger.warning(
                "audit_writer_wal_first_queue_unavailable",
                message="Audit rows will use structlog fallback; recorder queue is unavailable.",
            )
            return None
        from hft_platform.recorder.audit import RecorderQueueAuditWriter

        return RecorderQueueAuditWriter(queue)
    return getattr(recorder, "writer", None)


_STATE_NOT_SUPPLIED: Any = object()

DEFAULT_MTM_INCOMPLETE_REPEAT_TICKS = 3600


def _mtm_repeat_ticks_from_env() -> int:
    """Read ``HFT_MTM_INCOMPLETE_REPEAT_TICKS``; ``<= 0`` means edges only.

    Read once at construction rather than per tick, matching how the other
    supervisor intervals are resolved.
    """
    raw = os.getenv("HFT_MTM_INCOMPLETE_REPEAT_TICKS", "").strip()
    if not raw:
        return DEFAULT_MTM_INCOMPLETE_REPEAT_TICKS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "mtm_repeat_ticks_invalid",
            value=raw,
            using=DEFAULT_MTM_INCOMPLETE_REPEAT_TICKS,
        )
        return DEFAULT_MTM_INCOMPLETE_REPEAT_TICKS
    return max(0, value)


class HFTSystem:
    _drift_burst_symbol: str

    _loop_probe_peak_ms: float = 0.0
    _loop_probe_samples: int = 0

    _mtm_last_incomplete: bool | None = None
    _mtm_last_unpriced: tuple[str, ...] = ()
    _mtm_incomplete_ticks: int = 0
    _mtm_last_log_tick: int = 0
    _mtm_repeat_ticks: int = DEFAULT_MTM_INCOMPLETE_REPEAT_TICKS

    _LOSS_EXIT_MAX_BOOK_AGE_NS: int = 30 * 1_000_000_000

    _manual_latches_active: Mapping[str, str] = MappingProxyType({})
    _daily_loss_latch_published: bool = False
    _daily_loss_state_written: dict[str, Any] | None = None
    _operator_flatten_initial: Mapping[str, int] = MappingProxyType({})
    _operator_flatten_deadline_ns: int = 0
    _operator_flatten_request_id: int = 0
    _flatten_gate: FlattenGate | None = None
    _kill_flatten_blocked_logged: bool = False
    _kill_switch_actor: str = ""

    def _daily_loss_validator(self) -> DailyLossLimitValidator | None:
        risk_engine = getattr(self, "risk_engine", None)
        for v in getattr(risk_engine, "validators", None) or ():
            if isinstance(v, DailyLossLimitValidator):
                return v
        return None

    @staticmethod
    def _audit_engine_start(storm_guard: Any) -> None:
        """Write one ``old_state="BOOT"`` row to ``audit.guardrail_log``.

        A restart resets StormGuard to NORMAL without a transition, so the
        table read as if a HALT before a restart lasted until the next row:
        state durations across a boot were overstated, and a restart that
        re-armed trading left no trace. Written before the kill-switch and latch
        restores below, so their HALT rows sort after it.
        """
        try:
            from hft_platform.recorder.audit import get_audit_writer

            state = getattr(getattr(storm_guard, "state", None), "name", "UNKNOWN")
            get_audit_writer().log_guardrail_transition(
                {"old_state": "BOOT", "new_state": state, "reason": "engine_start"}
            )
        except Exception as exc:
            logger.warning("audit_engine_start_failed", error=str(exc))

    def _restore_daily_loss_state(self) -> None:
        """Load today's daily-loss state at boot, before anything can dispatch.

        A document from another trading day is ignored (the roll would have
        cleared it). An unreadable one is logged and ignored: the stops that
        must survive a restart are the manual latch files, read next.
        """
        validator = self._daily_loss_validator()
        if validator is None:
            return
        try:
            document = daily_loss_state.load()
            if document is None:
                return
            restored = validator.restore_state(document["validator"])
        except Exception as exc:
            logger.critical(
                "daily_loss_state_unreadable_at_boot",
                path=str(daily_loss_state.state_path()),
                error=str(exc),
                consequence="today's realized loss starts from zero",
            )
            return
        if restored:
            self._daily_loss_latch_published = bool(document.get("latch_published", False))
            logger.warning(
                "daily_loss_state_restored",
                halt_triggered=validator.halt_triggered,
                halt_reason=validator.halt_reason,
                accumulated=validator.export_state()["accumulated"],
            )
        else:
            logger.info("daily_loss_state_from_previous_day_ignored")

    def _apply_manual_latches(self, latches: Mapping[str, str]) -> None:
        """Hold HALT while any manual latch file is present; release when none is."""
        self._manual_latches_active = MappingProxyType(dict(latches))
        if latches and self.storm_guard.state != StormGuardState.HALT:
            summary = ", ".join(f"{name}={reason}" for name, reason in sorted(latches.items()))
            self.storm_guard.trigger_halt(f"MANUAL_LATCH: {summary}")
            logger.critical("Manual latch present - HALT", latches=dict(latches))
        self.storm_guard.set_manual_latch_hold(bool(latches))
        metrics = getattr(self.storm_guard, "metrics", None)
        gauge = getattr(metrics, "manual_latch_active", None)
        if gauge is not None:
            for name in manual_latches.LATCH_NAMES:
                gauge.labels(latch=name).set(1 if name in latches else 0)

    async def _sync_manual_latches(self) -> None:
        """Supervise-tick half of the durable latches. Disk IO runs in threads.

        1. A daily stop latched on the hard limit becomes a manual latch file,
           once per stop.
        2. The validator state is persisted when it changed.
        3. The latch files present set the hold. A failed read keeps the last
           known set: an IO error is not an operator's release.
        """
        validator = self._daily_loss_validator()
        if validator is not None:
            if not validator.halt_triggered:
                self._daily_loss_latch_published = False
            elif validator.halt_reason == "DAILY_LOSS_LIMIT" and not self._daily_loss_latch_published:
                try:
                    await asyncio.to_thread(
                        manual_latches.activate,
                        manual_latches.DAILY_LOSS,
                        "DAILY_LOSS_LIMIT",
                        actor="engine",
                    )
                    self._daily_loss_latch_published = True
                except Exception as exc:
                    logger.error("daily_loss_latch_write_failed", error=str(exc))
            document = {"validator": validator.export_state(), "latch_published": self._daily_loss_latch_published}
            if document != self._daily_loss_state_written:
                try:
                    await asyncio.to_thread(daily_loss_state.save, document)
                    self._daily_loss_state_written = document
                except Exception as exc:
                    logger.error("daily_loss_state_write_failed", error=str(exc))
            if not validator.halt_triggered and manual_latches.DAILY_LOSS in self._manual_latches_active:
                await self._release_daily_loss_latch_after_roll(int(validator.export_state()["boundary_ns"]))
        try:
            latches = await asyncio.to_thread(manual_latches.active)
        except Exception as exc:
            logger.error("manual_latch_read_failed", error=str(exc))
            latches = dict(self._manual_latches_active)
        self._apply_manual_latches(latches)

    @staticmethod
    async def _release_daily_loss_latch_after_roll(boundary_ns: int) -> None:
        """Release a daily-loss latch written before the current trading day began.

        The validator has already released its own stop for the day (it does
        so only on a complete valuation), so the latch is all that still holds
        HALT. A latch written after the 05:00 boundary is today's stop and
        stays. An unreadable latch has no date to compare and stays too.
        """

        def _before_roll(record: Any) -> bool:
            ts = record.get("timestamp_ns") if isinstance(record, dict) else None
            return type(ts) is int and ts < boundary_ns

        try:
            released = await asyncio.to_thread(
                manual_latches.release_when,
                manual_latches.DAILY_LOSS,
                _before_roll,
                reason="trading day rolled at 05:00 Taipei",
                actor="engine:day_roll",
            )
        except Exception as exc:
            logger.error("daily_loss_latch_release_failed", error=str(exc))
            return
        if released:
            logger.warning("daily_loss_latch_released_at_day_roll", boundary_ns=boundary_ns)

    def _run_loss_exit(self) -> None:
        """Submit the FORCE_FLAT intents ``LossExitController`` says are due.

        Inputs come from the validators that own the policy: the daily-loss
        validator's latched stop and per-position loss limit, and the expiry
        validator's cutoff. ``loss_exit.enabled: false`` in the risk config
        turns the exit off at the next SIGHUP reload, without a restart.
        """
        controller = getattr(self, "_loss_exit", None)
        risk_engine = getattr(self, "risk_engine", None)
        if controller is None or risk_engine is None:
            return
        operator_symbols = frozenset(self._operator_flatten_initial)
        auto_enabled = bool(
            ((getattr(risk_engine, "config", None) or {}).get("loss_exit", {}) or {}).get("enabled", True)
        )
        if not auto_enabled and not operator_symbols:
            return
        lob = getattr(self.md_service, "lob", None)
        if lob is None:
            return
        daily = expiry = None
        for v in risk_engine.validators:
            if isinstance(v, DailyLossLimitValidator):
                daily = v
            elif isinstance(v, ExpiringContractValidator):
                expiry = v
        now_ns = timebase.now_ns()
        max_age_ns = self._LOSS_EXIT_MAX_BOOK_AGE_NS

        def _l1(symbol: str) -> tuple[int, int] | None:
            top = lob.get_l1_scaled(symbol)
            if top is None or now_ns - int(top[0]) > max_age_ns:
                return None
            return int(top[1]), int(top[2])

        positions = [
            OpenPosition(p.strategy_id, p.symbol, int(p.net_qty), int(p.avg_price_scaled))
            for p in self.position_store.snapshot_open_positions().values()
            if p.net_qty
        ]
        kill = auto_enabled and bool(getattr(getattr(self, "storm_guard", None), "kill_switch_hold", False))
        kill_flatten = self._kill_switch_may_flatten(kill and bool(positions)) and kill
        meta = self.position_store.metadata
        intents = controller.plan(
            now_ns=now_ns,
            positions=positions,
            daily_halt=auto_enabled
            and (
                bool(daily is not None and daily.halt_triggered)
                or manual_latches.DAILY_LOSS in self._manual_latches_active
            ),
            position_loss_limit_scaled=daily.position_loss_limit_scaled if auto_enabled and daily is not None else 0,
            expiry_enabled=auto_enabled and bool(expiry is not None and expiry.enabled),
            expiry_cutoff_minute=expiry.cutoff_minute if expiry is not None else DEFAULT_EXPIRY_CUTOFF_MINUTE,
            l1=_l1,
            multiplier=meta.contract_multiplier,
            tick_size=meta.tick_size_scaled,
            operator_symbols=operator_symbols,
            kill_switch=kill_flatten,
        )
        if kill:
            intents.extend(controller.cancel_intents(self._resting_orders(), now_ns, trigger=TRIGGER_KILL_SWITCH))
        elif operator_symbols:
            resting = [o for o in self._resting_orders() if o[2] in operator_symbols]
            intents.extend(controller.cancel_intents(resting, now_ns))
        intent_channel = getattr(self, "intent_channel", None)
        gateway_on = getattr(self, "gateway_service", None) is not None and intent_channel is not None
        exit_queue: Any = intent_channel if gateway_on else self.risk_queue
        submit = getattr(exit_queue, "submit_nowait", None) or exit_queue.put_nowait
        for intent in intents:
            try:
                submit(intent)
            except Exception as e:
                logger.error(
                    "loss_exit_submit_failed",
                    symbol=intent.symbol,
                    strategy_id=intent.strategy_id,
                    reason=intent.reason,
                    error=str(e),
                )

    def _kill_switch_may_flatten(self, positions_open: bool) -> bool:
        """Whether the kill switch may close the book now, and say so when it may not.

        Only for a switch a person set (``_KILL_FLATTEN_ACTORS``; never the
        startup recovery gate, whose position store is not trusted) and only on
        evidence that the book is right: a reconciliation within
        ``max(15 s, 3 sync intervals)`` that found every platform position equal
        to the broker's. Otherwise the switch only stops trading; the gauge
        ``kill_switch_flatten_blocked`` (alert ``KillSwitchFlattenBlocked``)
        says positions are being held unclosed.
        """
        recon = getattr(self, "recon_service", None)
        verified_ns = int(getattr(recon, "positions_verified_mono_ns", 0) or 0)
        interval_s = float(getattr(recon, "check_interval_s", 5.0) or 5.0)
        max_age_ns = int(max(15.0, 3 * interval_s) * 1_000_000_000)
        verified = verified_ns > 0 and time.monotonic_ns() - verified_ns <= max_age_ns
        actor = self._kill_switch_actor
        by_operator = not getattr(self, "_recovery_halted", False) and actor in _KILL_FLATTEN_ACTORS
        blocked = positions_open and not (verified and by_operator)
        try:
            MetricsRegistry.get().kill_switch_flatten_blocked.set(1 if blocked else 0)
        except Exception as exc:
            logger.warning("kill_switch_flatten_blocked_gauge_failed", error=str(exc))
        if blocked and not self._kill_flatten_blocked_logged:
            logger.critical(
                "kill_switch_flatten_blocked",
                reason=(
                    "no recent reconciliation confirmed the positions against the broker"
                    if by_operator
                    else "the kill switch was not set by an operator"
                ),
                actor=actor,
                verified_age_s=(time.monotonic_ns() - verified_ns) / 1e9 if verified_ns else None,
            )
        self._kill_flatten_blocked_logged = blocked
        return verified and by_operator

    def _resting_orders(self) -> list[tuple[str, str, str]]:
        """``(order_key, strategy_id, symbol)`` for each order the broker holds open.

        Read on the event loop, where every writer of ``live_orders`` runs, so
        the copy is consistent. Entries still waiting for the broker's answer
        have no contract yet and are taken on a later tick.
        """
        live = getattr(getattr(self, "order_adapter", None), "live_orders", None) or {}
        orders: list[tuple[str, str, str]] = []
        for order_key, trade in list(live.items()):
            contract = getattr(trade, "contract", None)
            if contract is None:
                continue
            orders.append((str(order_key), str(order_key).split(":", 1)[0], str(getattr(contract, "code", "") or "")))
        return orders

    def _platform_net_by_symbol(self) -> dict[str, int]:
        """Platform-wide net quantity per symbol, recovered lots included.

        A FORCE_FLAT closes this whole net, whichever strategy holds it.
        """
        net: dict[str, int] = {}
        for p in self.position_store.snapshot_open_positions().values():
            if p.net_qty:
                net[p.symbol] = net.get(p.symbol, 0) + int(p.net_qty)
        return {sym: qty for sym, qty in net.items() if qty}

    async def _sync_operator_flatten(self) -> None:
        """Serve ``hft ops flatten``: claim the request, track it, write back the outcome.

        The orders themselves come from ``_run_loss_exit`` (``operator_flatten``
        trigger), so they take the same path, price protection and retry as
        every other platform exit. The request is finished from the position
        store, not from orders sent: COMPLETED once every named symbol is flat,
        or at the deadline with the symbols still open counted as partially
        closed or failed; FAILED at the deadline while an order is still
        unacknowledged. Every write-back names the request it answers. File IO
        runs in threads.
        """
        gate = self._flatten_gate
        if gate is None:
            gate = self._flatten_gate = FlattenGate()
        if self._operator_flatten_initial:
            await self._finish_operator_flatten_if_due(gate)
            return
        req = await asyncio.to_thread(gate.read_request)
        if req is None:
            return
        if req.status == FlattenStatus.PROCESSING:
            await asyncio.to_thread(
                gate.fail,
                "engine restarted before the flatten finished; positions not confirmed",
                request_id=req.initiated_ns,
            )
            logger.error("operator_flatten_abandoned_by_restart", scope=req.scope, scope_id=req.scope_id)
            return
        if req.status != FlattenStatus.PENDING:
            return
        req = await asyncio.to_thread(gate.claim)
        if req is None:
            return
        if req.scope not in ("all", "strategy") or (req.scope == "strategy" and not req.scope_id):
            await asyncio.to_thread(
                gate.fail,
                f"unsupported flatten scope {req.scope!r} (use all, or strategy with an id)",
                request_id=req.initiated_ns,
            )
            return
        net = self._platform_net_by_symbol()
        if req.scope == "strategy":
            held = {
                p.symbol
                for p in self.position_store.snapshot_open_positions().values()
                if p.net_qty and p.strategy_id == req.scope_id
            }
            net = {sym: qty for sym, qty in net.items() if sym in held}
        logger.warning(
            "operator_flatten_started",
            scope=req.scope,
            scope_id=req.scope_id,
            deadline_s=req.deadline_s,
            symbols=dict(net),
        )
        if not net:
            await asyncio.to_thread(gate.complete, 0, 0, 0, [], request_id=req.initiated_ns)
            return
        self._operator_flatten_initial = MappingProxyType(net)
        self._operator_flatten_deadline_ns = timebase.now_ns() + max(1, int(req.deadline_s)) * 1_000_000_000
        self._operator_flatten_request_id = req.initiated_ns

    async def _finish_operator_flatten_if_due(self, gate: FlattenGate) -> None:
        initial = self._operator_flatten_initial
        net = self._platform_net_by_symbol()
        remaining = {sym: net[sym] for sym in initial if net.get(sym, 0) != 0}
        live = getattr(getattr(self, "order_adapter", None), "live_orders", None) or {}
        unacked = any(
            getattr(trade, "contract", None) is None and not is_platform_exit(str(key)) for key, trade in live.items()
        )
        working = {sym for key, _, sym in self._resting_orders() if sym in initial and not is_platform_exit(key)}
        for sym in working:
            remaining.setdefault(sym, net.get(sym, 0))
        if (remaining or unacked) and timebase.now_ns() < self._operator_flatten_deadline_ns:
            return
        partial = sorted(sym for sym, qty in remaining.items() if abs(qty) < abs(initial[sym]))
        failed = sorted(sym for sym in remaining if sym not in partial)
        request_id = self._operator_flatten_request_id
        if unacked:
            written = await asyncio.to_thread(
                gate.fail,
                "orders still awaiting the broker's acknowledgement at the deadline; positions not confirmed",
                sorted(remaining),
                request_id=request_id,
            )
        else:
            written = await asyncio.to_thread(
                gate.complete,
                len(initial) - len(remaining),
                len(partial),
                len(failed),
                sorted(remaining),
                request_id=request_id,
            )
        if not written:
            logger.warning("operator_flatten_superseded", request_id=self._operator_flatten_request_id)
        log = logger.error if remaining or unacked else logger.warning
        log(
            "operator_flatten_finished",
            unacknowledged_orders=unacked,
            fully_closed=len(initial) - len(remaining),
            partially_closed=partial,
            failed=failed,
            still_open=remaining,
        )
        self._operator_flatten_initial = MappingProxyType({})
        self._operator_flatten_deadline_ns = 0
        self._operator_flatten_request_id = 0

    @staticmethod
    def _get_max_feed_gap_s(md_service: Any) -> float:
        """Return max feed gap from market data service, or 0.0 if unavailable."""
        client = getattr(md_service, "client", None)
        if client is not None and hasattr(client, "get_healthy_feed_gap_s"):
            gap = client.get_healthy_feed_gap_s()
            within_fn = getattr(md_service, "within_reconnect_window", None)
            if within_fn is not None and not within_fn():
                return 0.0
            return float(gap)
        fn = getattr(md_service, "get_max_feed_gap_s", None)
        if fn is None:
            return 0.0
        gap = fn()
        within_fn = getattr(md_service, "within_reconnect_window", None)
        if within_fn is not None and not within_fn():
            return 0.0
        return float(gap)

    @staticmethod
    def _get_feed_gaps_by_symbol(md_service: Any) -> Dict[str, float]:
        """Return per-symbol feed gaps, or empty dict if unavailable."""
        fn = getattr(md_service, "get_feed_gaps_by_symbol", None)
        if fn is None:
            return {}
        return fn()

    def _log_mtm_completeness(self, mtm: Any) -> None:
        """Say whether the book could be priced -- on the edge, with names.

        The supervisor ticks at 1 Hz, and this used to warn on every tick a
        position lacked a mid. On THESHOW a single stale ``TMFI6`` position
        held that state from 2026-09-07 to 2026-09-19 and produced 97,497
        ``mark-to-market incomplete`` lines in 48 hours, alongside 97,508
        ``mid_price_unavailable`` lines from the calculator -- together ~65% of
        every line the engine wrote. The condition was real and worth knowing;
        saying it 86,400 times a day is what made it unfindable, and it never
        named the instrument, so the log could not answer the only question an
        operator has.

            BEFORE (1 Hz, anonymous):
              t+0s   mark-to-market incomplete  priced=0 unpriced=1
              t+1s   mark-to-market incomplete  priced=0 unpriced=1
              ...    x 86,400 per day, x 13 days     <- which symbol? X

            AFTER (edges + hourly reminder, named):
              t+0s   mtm_incomplete        unpriced=1 symbols=[TMFI6]
              t+1h   mtm_still_incomplete  ticks_incomplete=3600 symbols=[TMFI6]
              ...    (quiet)
              t+13d  mtm_complete          priced=1   <- recovery is an event too

        Three things break the silence, because each is new information:
        entering the state, the *set* of unpriced symbols changing while in it,
        and leaving it. A periodic reminder is kept on purpose -- a condition
        that blocks the daily stop from releasing must not be invisible to
        ``docker logs --since 1h``.
        """
        incomplete = not mtm.complete
        symbols = tuple(getattr(mtm, "unpriced_symbols", ()) or ())
        was_incomplete = self._mtm_last_incomplete

        if incomplete:
            if was_incomplete is not True:
                self._mtm_incomplete_ticks = 0
                self._mtm_last_log_tick = 0
            self._mtm_incomplete_ticks += 1
            changed = was_incomplete is not True or symbols != self._mtm_last_unpriced
            interval = self._mtm_repeat_ticks
            due = interval > 0 and self._mtm_incomplete_ticks - self._mtm_last_log_tick >= interval
            if changed or due:
                logger.warning(
                    "mtm_incomplete" if changed else "mtm_still_incomplete",
                    priced=mtm.priced,
                    unpriced=mtm.unpriced,
                    symbols=list(symbols),
                    ticks_incomplete=self._mtm_incomplete_ticks,
                    hint="daily stop cannot release while the book is unpriceable",
                )
                self._mtm_last_log_tick = self._mtm_incomplete_ticks
        elif was_incomplete:
            logger.info(
                "mtm_complete",
                priced=mtm.priced,
                recovered_symbols=list(self._mtm_last_unpriced),
                ticks_incomplete=self._mtm_incomplete_ticks,
            )
            self._mtm_incomplete_ticks = 0
            self._mtm_last_log_tick = 0

        self._mtm_last_incomplete = incomplete
        self._mtm_last_unpriced = symbols

    @staticmethod
    def _set_service_running(service: Any, value: bool) -> None:
        """Set the ``running`` attribute on *service* if it exists."""
        if hasattr(service, "running"):
            service.running = value

    def _drift_burst_book(self, lob_engine: Any) -> Any | None:
        """Return the book of the latched drift-burst reference instrument.

        The drift-burst detector accumulates log-returns of a single price
        series. Feeding it a rotating symbol makes its t-statistic measure the
        jump between two contracts, which is how a platform-wide toxicity HALT
        ended up being driven by dict iteration order: ``lob_engine.books``
        evicts stale symbols, so the "first" entry changed over time.

        The latch is held for as long as the instrument still quotes; when it
        is evicted or goes bid-less a new one is picked and the change is
        logged, so the escalation is always attributable to a named symbol.
        """
        books = getattr(lob_engine, "books", None)
        if not books:
            return None

        current = getattr(self, "_drift_burst_symbol", "")
        if current:
            book = books.get(current)
            if book is not None and book.mid_price_x2 > 0:
                return book

        fallback_symbol = ""
        fallback_book: Any | None = None
        best_symbol = ""
        best_book: Any | None = None
        best_spread = 0
        best_mid_x2 = 0
        for symbol, book in books.items():
            mid_x2 = int(book.mid_price_x2)
            if mid_x2 <= 0:
                continue
            if fallback_book is None:
                fallback_symbol, fallback_book = symbol, book
            spread = int(book.spread)
            if spread <= 0:
                continue
            if best_book is None or spread * best_mid_x2 < best_spread * mid_x2:
                best_symbol, best_book, best_spread, best_mid_x2 = symbol, book, spread, mid_x2

        if best_book is not None:
            chosen_symbol, chosen_book, chosen_spread = best_symbol, best_book, best_spread
        elif fallback_book is not None:
            chosen_symbol, chosen_book, chosen_spread = fallback_symbol, fallback_book, 0
        else:
            return None

        logger.info(
            "drift_burst_reference_symbol_changed",
            previous=current or None,
            symbol=chosen_symbol,
            spread_scaled=chosen_spread,
        )
        self._drift_burst_symbol = chosen_symbol
        return chosen_book

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        configure_logging()
        self.settings = settings or {}
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_watchdog: LoopStallWatchdog | None = None
        self._recorder_seen_tick = False
        self._recorder_seen_bidask = False
        self._md_record_direct = os.getenv("HFT_MD_RECORD_DIRECT", "1").lower() not in {"0", "false", "no", "off"}
        self._fill_record_direct = True
        self._order_record_direct = True

        self.bootstrapper = SystemBootstrapper(self.settings)
        self.registry = self.bootstrapper.build()
        self.order_mode = resolve_order_mode()

        self.bus = self.registry.bus
        self.raw_queue = self.registry.raw_queue
        self.raw_exec_queue = self.registry.raw_exec_queue
        self._exec_overflow_buf: collections.deque = collections.deque(maxlen=4096)
        self._EXEC_OVERFLOW_MAX: int = 4096
        self._exec_overflow_counter: int = 0
        self._exec_overflow_evicted: int = 0
        self._foreign_rearm_request_ids: set[str] = set()
        self._exec_startup_overflow_lost: bool = False
        self.risk_queue = self.registry.risk_queue
        self.order_queue = self.registry.order_queue
        self.recorder_queue = self.registry.recorder_queue

        self.position_store = self.registry.position_store
        self.order_id_map = self.registry.order_id_map
        self.storm_guard = self.registry.storm_guard
        self.md_client = self.registry.md_client
        self.order_client = self.registry.order_client
        self.client = self.registry.client
        self.symbol_metadata = self.registry.symbol_metadata
        self.price_scale_provider = self.registry.price_scale_provider

        self.md_service = self.registry.md_service
        self.order_adapter = self.registry.order_adapter
        self.execution_gateway = self.registry.execution_gateway
        self.exec_service = self.registry.exec_service
        if hasattr(self.exec_service, "set_overflow_buf"):
            self.exec_service.set_overflow_buf(self._exec_overflow_buf)
        else:
            self.exec_service._overflow_buf = self._exec_overflow_buf
        self.risk_engine = self.registry.risk_engine
        self.recon_service = self.registry.recon_service
        self.strategy_runner = self.registry.strategy_runner
        self.recorder = self.registry.recorder
        self.gateway_service = self.registry.gateway_service
        self.intent_channel = getattr(self.registry, "intent_channel", None)
        self.checkpoint_writer = getattr(self.registry, "checkpoint_writer", None)
        self.startup_verifier = getattr(self.registry, "startup_verifier", None)
        self.startup_fill_reconciler = getattr(self.registry, "startup_fill_reconciler", None)
        self.session_governor = getattr(self.registry, "session_governor", None)
        self.autonomy_monitor = getattr(self.registry, "autonomy_monitor", None)
        self.daily_report_service = getattr(self.registry, "daily_report_service", None)
        self.position_stuck_monitor = getattr(self.registry, "position_stuck_monitor", None)
        self.evidence_writer = getattr(self.registry, "evidence_writer", None) or get_shared_autonomy_evidence_writer()
        self.platform_degrade_controller = (
            getattr(self.registry, "platform_degrade_controller", None) or get_shared_platform_degrade_controller()
        )
        self.manual_rearm_gate = ManualRearmGate()
        self._last_platform_rearm_request_seen = 0.0

        self.platform_degrade_inputs = getattr(
            self.registry, "platform_degrade_inputs", None
        ) or self.bootstrapper.build_platform_degrade_inputs(
            md_service=self.md_service,
            recorder=self.recorder,
            raw_queue=self.raw_queue,
            raw_exec_queue=self.raw_exec_queue,
            recorder_queue=self.recorder_queue,
            risk_queue=self.risk_queue,
            order_queue=self.order_queue,
        )
        self.platform_degrade_inputs.bind_runtime_probes(
            redis_client=getattr(self, "redis_client", None),
            redis_healthcheck=getattr(self, "redis_healthcheck", None),
        )
        self.platform_degrade_controller.evidence_writer = self.evidence_writer
        self.order_adapter.platform_degrade_controller = self.platform_degrade_controller
        self.order_adapter.position_store = self.position_store
        if hasattr(self.order_adapter, "set_storm_guard"):
            self.order_adapter.set_storm_guard(self.storm_guard)
        else:
            self.order_adapter._storm_guard = self.storm_guard

        if hasattr(self.md_service, "register_on_reconnect"):
            self.md_service.register_on_reconnect(
                lambda reason: self.order_adapter.invalidate_live_orders(reason=reason)
            )
            self.md_service.register_on_reconnect(lambda reason: self.strategy_runner.reset_stale_counter())
        self.recon_service.platform_degrade_controller = self.platform_degrade_controller

        self._halt_log_mono: float = 0.0
        self._halt_episode_start_mono: float | None = None
        self._halt_checkpoint_written: bool = False
        self._recovery_halted: bool = False
        self._drift_burst_symbol = ""
        self._mtm_calculator = None
        self._mtm_last_incomplete = None
        self._mtm_last_unpriced = ()
        self._mtm_incomplete_ticks = 0
        self._mtm_last_log_tick = 0
        self._mtm_repeat_ticks = _mtm_repeat_ticks_from_env()
        try:
            from hft_platform.execution.mtm import MarkToMarketCalculator

            lob_engine = getattr(self.md_service, "lob", None)
            if lob_engine is not None:
                self._mtm_calculator = MarkToMarketCalculator(
                    self.position_store,
                    mid_price_fn=getattr(lob_engine, "get_mid_price", lambda s: None),
                    multiplier_fn=self.position_store.metadata.contract_multiplier,
                )
        except Exception as exc:
            logger.warning("MTM calculator init failed", error=str(exc))
        _exit_cfg = (getattr(self.risk_engine, "config", None) or {}).get("loss_exit", {}) or {}
        self._loss_exit = LossExitController(
            protection_ticks=int(_exit_cfg.get("protection_ticks", 5)),
            retry_interval_s=float(_exit_cfg.get("retry_interval_s", 10.0)),
        )

        self.tasks: Dict[str, asyncio.Task[Any]] = {}
        self._recorder_drop_on_full = os.getenv("HFT_RECORDER_DROP_ON_FULL", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._bootstrap_torn_down = False
        self._task_restart_attempts: Dict[str, int] = {}
        self._task_restart_until_s: Dict[str, float] = {}
        self._task_started_at: Dict[str, float] = {}
        self._task_restart_base_delay_s = self._env_float("HFT_TASK_RESTART_BACKOFF_S", 1.0, min_value=0.1)
        self._task_restart_max_delay_s = self._env_float("HFT_TASK_RESTART_BACKOFF_MAX_S", 30.0, min_value=0.1)
        self._task_restart_max_attempts = int(os.getenv("HFT_TASK_RESTART_MAX_ATTEMPTS", "10"))
        self._task_healthy_uptime_s = self._env_float("HFT_TASK_HEALTHY_UPTIME_S", 60.0, min_value=5.0)
        self._queue_log_every_s = self._env_float("HFT_SUPERVISOR_QUEUE_LOG_EVERY_S", 30.0, min_value=1.0)
        self._last_queue_log_s = 0.0
        self._recorder_bridge_drops: int = 0
        self._pnl_snapshot_drops: int = 0

        self.session_hook_manager = SessionHookManager()

        self.health_server = HealthServer(system=self)

        self._stop_async_task: asyncio.Task[None] | None = None

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()

        sg = getattr(self, "storm_guard", None)
        if sg is not None and hasattr(sg, "bind_loop"):
            sg.bind_loop(self.loop)
        self._audit_engine_start(sg)

        self._deferred_tasks: list[asyncio.Task[Any]] = []
        registry = getattr(self, "registry", None)
        for coro in list(getattr(registry, "deferred_tasks", None) or []):
            try:
                self._deferred_tasks.append(self.loop.create_task(coro))
            except Exception as exc:
                logger.warning("deferred_task_schedule_failed", error=str(exc))

        if self._exec_startup_overflow_lost:
            logger.critical(
                "exec_startup_overflow_halt",
                msg="Fills were LOST during startup race — triggering HALT",
                evicted_count=self._exec_overflow_evicted,
            )
            self.storm_guard.trigger_halt("exec_overflow_startup_race")

        if kill_switch.is_active():
            try:
                _boot_ks_reason = kill_switch.read_reason()
            except Exception as exc:
                logger.warning("kill_switch_unreadable_at_boot", error=str(exc))
                _boot_ks_reason = "kill_switch_file_present"
            self.storm_guard.trigger_halt(f"KILL_SWITCH_FILE: {_boot_ks_reason}")
            self.storm_guard.set_kill_switch_hold(True)
            logger.critical(
                "Kill switch active at startup - booting into HALT",
                path=kill_switch.kill_switch_path(),
                reason=_boot_ks_reason,
                consequence="feeds and recorder start; no new orders; cancels still allowed",
            )

        self._restore_daily_loss_state()
        try:
            _boot_latches = manual_latches.active()
        except Exception as exc:
            logger.critical("manual_latch_dir_unreadable_at_boot", error=str(exc))
            _boot_latches = {"unreadable": str(exc)}
        if _boot_latches:
            logger.critical(
                "Manual latch present at startup - booting into HALT",
                latches=_boot_latches,
                release="hft risk rearm <latch> --reason ...",
            )
        self._apply_manual_latches(_boot_latches)
        _gc_disabled = False

        import signal

        try:
            self.loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        except (NotImplementedError, OSError):
            pass

        logger.info("System Starting...")
        self.evidence_writer.record_transition(
            scope="platform",
            mode="NORMAL",
            reason="system_start",
            manual_rearm_required=False,
        )

        try:
            self._start_service("health_server", self.health_server.run())

            order_mode = getattr(self, "order_mode", None)
            if order_mode is None:
                order_mode = resolve_order_mode()
                self.order_mode = order_mode
            orders_enabled = order_mode != "disabled"

            if orders_enabled:
                login_ok = False
                try:
                    loop = asyncio.get_running_loop()
                    login_ok = await loop.run_in_executor(None, self.order_client.login)
                except Exception as exc:
                    logger.error("order_client login failed — orders will be unavailable", error=str(exc))

                if login_ok is True:
                    logger.info(
                        "order_client logged in",
                        contracts_ready=getattr(self.order_client, "contracts_ready", "N/A"),
                    )
                    self._exec_callbacks = (
                        lambda state, payload: self._on_exec("order", {"state": state, "payload": payload}),
                        lambda payload: self._on_exec("deal", {"payload": payload}),
                    )
                    self.order_client.set_execution_callbacks(
                        on_order=self._exec_callbacks[0],
                        on_deal=self._exec_callbacks[1],
                    )
                else:
                    logger.error("order_client login returned false — orders are unavailable", order_mode=order_mode)
                    if order_mode == "live":
                        logger.critical("live_order_login_failed_startup_blocked")
                        return
            else:
                logger.warning("order_path_disabled_quote_only")

            if orders_enabled and self.session_governor is not None:
                await self.session_governor.start()
                logger.info("SessionGovernor started")

            if orders_enabled and self.autonomy_monitor is not None:
                self._start_service("autonomy_monitor", self.autonomy_monitor.run())

            if orders_enabled and self.position_stuck_monitor is not None:
                self._start_service("position_stuck_monitor", self.position_stuck_monitor.run())

            self._start_service("recorder", self.recorder.run())
            pre_md_cursor = self.bus.cursor
            self._start_service("md", self.md_service.run())
            if orders_enabled and os.getenv("HFT_STARTUP_RECON_ENABLED", "1") == "1" and self.startup_verifier:
                self._bind_checkpoint_identity()
                try:
                    recovery = await self.startup_verifier.recover(
                        account_id=self.registry.account_id or self.registry.broker_id,
                    )
                    if recovery.halted:
                        self._on_startup_recovery_halt(
                            reason=f"startup_position_recovery:{recovery.source}",
                            source=recovery.source,
                            mismatches=recovery.mismatches,
                        )
                    logger.info(
                        "Position recovery complete",
                        source=recovery.source,
                        loaded=recovery.positions_loaded,
                        corrected=recovery.auto_corrected,
                    )
                except Exception as exc:
                    self._on_startup_recovery_halt(
                        reason="startup_position_recovery_failed",
                        source="exception",
                        mismatches=[],
                        error=str(exc),
                    )

            if (
                orders_enabled
                and os.getenv("HFT_STARTUP_RECON_ENABLED", "1") == "1"
                and self.startup_fill_reconciler is not None
            ):
                try:
                    fill_backfill = await self.startup_fill_reconciler.run()
                    logger.info(
                        "Startup fill reconciliation complete",
                        broker_fills=fill_backfill.broker_fills,
                        platform_fills=fill_backfill.platform_fills,
                        inserted=fill_backfill.inserted,
                        broker_query_error=fill_backfill.broker_query_error,
                        errors=len(fill_backfill.errors),
                    )
                except Exception as exc:
                    logger.warning("Startup fill reconciliation failed", error=str(exc))

            if orders_enabled:
                self._start_service("exec_router", self.exec_service.run())
                if self.gateway_service is not None:
                    self._start_service("gateway", self.gateway_service.run())
                else:
                    self._start_service("risk", self.risk_engine.run())
                self._start_service("order", self.order_adapter.run())
                self._start_service("exec_gateway", self.execution_gateway.run())
                self._start_service("order_session_watchdog", self._order_session_watchdog())

                if self._recovery_halted:
                    logger.critical(
                        "checkpoint_writer_suppressed_after_recovery_halt",
                        consequence="the checkpoint is preserved exactly as recovery found it",
                    )
                elif os.getenv("HFT_CHECKPOINT_ENABLED", "1") == "1" and self.checkpoint_writer:
                    self._start_service("checkpoint_writer", self.checkpoint_writer.run())

                self._start_service("recon", self.recon_service.run())
                self.strategy_runner.set_start_cursor(pre_md_cursor)
                self._start_service("strat", self.strategy_runner.run())
                if (
                    hasattr(self.strategy_runner, "_rejection_queue")
                    and self.strategy_runner._rejection_queue is not None
                ):
                    self._start_service("rejection_consumer", self.strategy_runner._run_rejection_consumer())
            else:
                logger.info("quote_only_service_plane_active", services=["health_server", "recorder", "md"])

            try:
                from hft_platform.recorder.audit import get_audit_writer

                self._audit_writer = get_audit_writer()
                _recorder_writer = _audit_persistence_writer_for_recorder(self.recorder)
                if _recorder_writer is not None and hasattr(self._audit_writer, "set_writer"):
                    self._audit_writer.set_writer(_recorder_writer)
                await self._audit_writer.start()
                if getattr(self._audit_writer, "_writer", None) is None:
                    logger.warning(
                        "audit_writer_persistence_mode_structlog",
                        message=(
                            "AuditWriter has no ClickHouse writer attached; "
                            "audit rows will be emitted as structlog "
                            "audit_fallback events only (audit.* tables stay empty)."
                        ),
                    )
                else:
                    _audit_persistence = (
                        "wal_first_recorder_queue"
                        if type(_recorder_writer).__name__ == "RecorderQueueAuditWriter"
                        else "clickhouse"
                    )
                    logger.info(
                        "audit_writer_started",
                        persistence=_audit_persistence,
                        writer_type=type(_recorder_writer).__name__,
                    )
                if hasattr(self.order_adapter, "set_audit_writer"):
                    self.order_adapter.set_audit_writer(self._audit_writer)
            except Exception as exc:
                logger.error("AuditWriter start failed — audit trail unavailable", error=str(exc))
                self._audit_writer = None
            if self._md_record_direct and self._fill_record_direct and self._order_record_direct:
                logger.info(
                    "recorder_bridge_skipped",
                    reason="all_direct_recording_enabled",
                )
            else:
                self._start_service("recorder_bridge", self._recorder_bridge())
            if orders_enabled and os.getenv("HFT_PNL_EXPORTER_ENABLED", "1").lower() not in {
                "0",
                "false",
                "no",
                "off",
            }:
                self._start_service("pnl_exporter", self._pnl_snapshot_exporter())

            if orders_enabled and self.session_hook_manager.enabled:
                self._start_service("session_hooks", self.session_hook_manager.run())

            if os.getenv("HFT_GC_DISABLE_TRADING", "0").strip().lower() in {"1", "true", "yes", "on"}:
                gc.disable()
                _gc_disabled = True
                logger.info("GC disabled for trading session")

            self._loop_watchdog = LoopStallWatchdog(
                stall_kill_s=self._env_float("HFT_LOOP_STALL_KILL_S", 60.0, 0.0),
                check_interval_s=self._env_float("HFT_LOOP_STALL_CHECK_S", 5.0, 0.1),
                warn_stall_s=self._env_float("HFT_LOOP_STALL_WARN_S", 15.0, 0.0),
            )
            self._loop_watchdog.start()

            self._start_service("loop_probe", self._probe_event_loop_lag())

            await self._supervise()

        except asyncio.CancelledError:
            logger.info("System Stopping...")
        finally:
            if _gc_disabled:
                gc.enable()
                logger.info("GC re-enabled after trading session")
            await self.stop_async()

    def _bind_checkpoint_identity(self) -> bool:
        """Name the logged-in account in the checkpoint identity, before recovery reads it.

        ``bootstrap.build()`` runs before the broker login, when the client
        knows no account yet, so its identity fell back to the broker id and
        every account shared one (Codex review of B0-B6, 2026-10-24): another
        account's checkpoint restored as this one's. Returns False when the
        account is still unknown; the identity then carries an empty account,
        which no checkpoint written with a known account matches, so recovery
        falls back to the broker snapshot rather than vouch for a stranger's.
        """
        from hft_platform.execution.checkpoint import checkpoint_identity

        try:
            account_id = str(getattr(self.order_client, "get_default_account_id", lambda: "")() or "")
        except Exception as exc:
            logger.warning("checkpoint_identity_account_lookup_failed", error=str(exc))
            account_id = ""
        identity = checkpoint_identity(
            broker_id=str(getattr(self.registry, "broker_id", "") or ""),
            account_id=account_id,
            order_mode=os.getenv("HFT_ORDER_MODE", ""),
        )
        for holder in (getattr(self, "checkpoint_writer", None), getattr(self, "startup_verifier", None)):
            if holder is not None:
                holder.identity = identity
        if not account_id:
            logger.critical(
                "checkpoint_identity_account_unknown",
                consequence="no checkpoint stamped with an account is restored; recovery uses the broker snapshot",
            )
            return False
        return True

    def _on_startup_recovery_halt(
        self,
        *,
        reason: str,
        source: str,
        mismatches: list[dict[str, Any]],
        error: str | None = None,
    ) -> None:
        """Refuse to trade when startup recovery could not establish position truth.

        This was a bare ``return`` out of ``run()``. Under the container's
        ``restart: always`` policy that is not fail-closed -- the process exited
        0, Docker read success, restarted, and the second boot found a
        checkpoint the shutdown path had just rewritten from an empty store::

            BEFORE:  halt -> return -> exit 0 -> restart -> trade, mismatch gone
            AFTER:   halt -> latch file + StormGuard HALT, checkpoint untouched

        Observed in production 2026-09-04: boot 1 at 14:06:36Z halted on
        checkpoint -4 vs broker 0, exited 0 at 14:07:53Z, and boot 2 at
        14:08:01Z logged "Position recovery complete, loaded=0" and started
        trading. ``RestartCount`` went 0 -> 1; nothing in the logs said restart.

        Booting on is deliberate. A crash loop would take the feeds, the
        recorder and every metric down with it and leave the operator blind to
        the mismatch that needs resolving, while HALT already blocks new orders
        and still allows cancels. The latch is what makes it survive a restart;
        clearing it is an operator action (``hft risk resume``).
        """
        self._recovery_halted = True
        logger.critical(
            "Position recovery HALT - refusing to trade",
            source=source,
            mismatches=mismatches,
            error=error,
        )
        try:
            path = kill_switch.activate(reason, actor="startup_position_recovery")
            logger.critical("kill_switch_latched_by_startup_recovery", path=path, reason=reason)
        except OSError as exc:
            logger.critical("kill_switch_latch_write_failed", error=str(exc), reason=reason)
        self.storm_guard.trigger_halt(f"STARTUP_POSITION_RECOVERY: {source}")
        self.storm_guard.set_kill_switch_hold(True)

    def _release_halt_drained(self, item: object, reason: str = "HALT_DRAINED") -> None:
        """Tell the owning strategy that a drained intent/command was dropped.

        The HALT drains discard work without telling anyone. A strategy that
        counted the item as in flight (R47's ``_pending_sell``) then holds the
        slot forever: 2026-09-29 two drained reduce-only SELLs left
        ``pending_sell=2`` and ``can_sell`` False for 79 h, until a restart.
        ``side`` is mandatory in the feedback; without it ``on_risk_feedback``
        refuses to release (Bug 9).
        """
        queue = getattr(self.strategy_runner, "_rejection_queue", None)
        if queue is None:
            return
        intent = getattr(item, "intent", None)
        if intent is None:
            intent = getattr(item, "payload", item)
        side = typed_intent_side(intent)
        strategy_id = typed_intent_strategy_id(intent)
        if side is None or not strategy_id:
            logger.warning("halt_drain_feedback_unaddressable", reason=reason, item_type=type(item).__name__)
            return
        try:
            queue.put_nowait(
                RiskFeedback(
                    intent_id=typed_intent_id(intent),
                    strategy_id=strategy_id,
                    symbol=typed_intent_symbol(intent),
                    reason_code=reason,
                    timestamp_ns=timebase.now_ns(),
                    side=Side(side),
                )
            )
        except (asyncio.QueueFull, ValueError):
            logger.warning("halt_drain_feedback_dropped", strategy_id=strategy_id, reason=reason)

    def _halt_status_log_kind(self, now_mono: float, interval_s: float = 60.0) -> str:
        """Decide what this HALT tick should log: ``entry``, ``heartbeat`` or ``""``.

        A HALT is one condition, not one condition per minute. Until 2026-09-09
        every tick past the rate limiter emitted the same field-less line at
        ``error``, so a single four-hour HALT on THESHOW produced 239 of that
        day's 240 error records. That makes every error-rate signal -- an alert,
        a dashboard, or a "no errors in N days" claim -- a measure of how long a
        HALT lasted rather than of how many things went wrong, and it buried the
        one unrelated error of the day (a ``place_order`` timeout) under 239
        copies of a fact already reported by ``StormGuard Transition`` and by
        the ``StormGuardHalt`` alert.

        Announce the episode once at ``error``; keep saying so at ``warning``,
        carrying the age that makes the repetition worth reading.

        Advances the rate-limit state, so call it once per tick.
        """
        if self._halt_episode_start_mono is None:
            self._halt_episode_start_mono = now_mono
            self._halt_log_mono = now_mono
            return "entry"
        if now_mono - self._halt_log_mono >= interval_s:
            self._halt_log_mono = now_mono
            return "heartbeat"
        return ""

    def _write_halt_entry_checkpoint(self) -> None:
        """M5: persist positions once per HALT episode, on entry, not every tick.

        Skipped entirely after a startup recovery halt: that HALT is *caused* by
        the checkpoint, and the store behind this write holds nothing, so the
        write would destroy the record that produced it.
        """
        if self._recovery_halted or self._halt_checkpoint_written:
            return
        if self.checkpoint_writer is None:
            return
        self._halt_checkpoint_written = True
        try:
            self.checkpoint_writer.write_checkpoint()
        except Exception:
            logger.exception("halt_checkpoint_write_failed")

    def _write_final_checkpoint(self) -> None:
        """Persist positions on shutdown, unless recovery refused to trust them.

        On the recovery-halt path this is the single most damaging write in the
        system: the store is empty by construction, so it overwrites the very
        checkpoint that refused the boot and the next boot finds nothing left to
        halt on. That is how the 2026-09-04 mismatch was lost.
        """
        if self._recovery_halted:
            logger.critical(
                "final_checkpoint_skipped_after_recovery_halt",
                consequence="checkpoint left exactly as recovery found it",
            )
            return
        if self.checkpoint_writer is None:
            return
        try:
            self.checkpoint_writer.write_checkpoint()
            logger.info("Final position checkpoint written")
        except Exception as exc:
            logger.warning("Final checkpoint failed", error=str(exc))

    def _start_service(self, name, coro):
        if name in {"exec_router", "exec_gateway"}:
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                metrics = MetricsRegistry.get()
                if name == "exec_router":
                    metrics.execution_router_alive.set(1)
                elif name == "exec_gateway":
                    metrics.execution_gateway_alive.set(1)
            except Exception as _exc:
                pass
        self.tasks[name] = asyncio.create_task(coro)
        self._task_started_at[name] = timebase.now_s()

    @staticmethod
    def _env_float(name: str, default: float, min_value: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            value = default
        return max(min_value, value)

    def _close_broker_client(self, client_name: str) -> None:
        """Close a broker client with logout if available."""
        client = getattr(self, client_name, None)
        if client is not None and hasattr(client, "close"):
            try:
                client.close(logout=True)
                logger.info("Broker client closed", client=client_name)
            except Exception as exc:
                logger.warning("Broker logout failed", client=client_name, error=str(exc))

    def _on_sighup(self) -> None:
        """Handle SIGHUP: reload risk config."""
        logger.info("SIGHUP received - reloading risk config")
        try:
            self.risk_engine.reload_config()
        except Exception as exc:
            logger.error("SIGHUP risk config reload failed", error=str(exc))

    def _sync_drain_recorder(self) -> None:
        """Best-effort synchronous recorder flush when event loop is unavailable.

        Creates a temporary event loop to:
        1. Drain recorder.queue into batchers (items not yet consumed by run loop).
        2. Flush batchers and shut down writer.
        This prevents silent data loss of fills/orders during synchronous stop()
        when the main event loop is not running (INFRA-015).

        P0-I5 (2026-04-24): BEFORE creating the temp loop, stop any background
        ``WALBatchWriter._timer_thread`` that may still be calling
        ``_write_batch_sync`` concurrently. The temp-loop drain path writes to
        the same WAL directory and file-sequence counter — a concurrent timer
        thread would create duplicate/racing files (corrupted renames, shared
        ``itertools.count`` access, cross-thread fsync).
        """
        recorder = getattr(self, "recorder", None)
        if recorder is None:
            return
        if getattr(recorder, "_shutdown_drained", False) is True:
            logger.info("sync_drain_recorder_skipped_already_drained")
            return

        self._stop_wal_batch_timers(recorder)

        recorder.running = False
        try:
            tmp_loop = asyncio.new_event_loop()
            try:
                _timeout = float(os.getenv("HFT_RECORDER_SHUTDOWN_TIMEOUT_S", "60"))

                async def _drain_and_flush() -> None:
                    await recorder._drain_queue_into_batchers()
                    await recorder._shutdown_flush()

                tmp_loop.run_until_complete(asyncio.wait_for(_drain_and_flush(), timeout=_timeout))
                recorder._shutdown_drained = True
                logger.info("Synchronous recorder drain complete")
            except Exception as exc:
                logger.warning("Synchronous recorder drain failed", error=str(exc))
            finally:
                tmp_loop.close()
        except Exception as exc:
            logger.warning("Synchronous recorder drain setup failed", error=str(exc))

    @staticmethod
    def _stop_wal_batch_timers(recorder: Any) -> None:
        """Stop all WALBatchWriter timer threads embedded in the recorder.

        Covers both DataWriter-embedded and WAL-first writer variants. Safe to
        call when writers are absent or already stopped — individual failures
        are logged but do not propagate.
        """
        writer = getattr(recorder, "writer", None)
        wal_batch_writer = getattr(writer, "_wal_batch_writer", None) if writer is not None else None
        if wal_batch_writer is not None and hasattr(wal_batch_writer, "stop"):
            try:
                wal_batch_writer.stop()
            except Exception as exc:
                logger.warning("wal_batch_writer_stop_failed", error=str(exc))

        wal_first = getattr(recorder, "_wal_first_writer", None)
        if wal_first is not None:
            inner = getattr(wal_first, "_wal", None)
            if inner is not None and hasattr(inner, "stop"):
                try:
                    inner.stop()
                except Exception as exc:
                    logger.warning("wal_first_batch_writer_stop_failed", error=str(exc))

    def _teardown_bootstrap(self) -> None:
        if self._bootstrap_torn_down:
            return
        self._bootstrap_torn_down = True
        for cn in ("md_client", "order_client"):
            self._close_broker_client(cn)
        try:
            self.bootstrapper.teardown()
        except Exception as exc:
            logger.warning("Bootstrap teardown failed", error=str(exc))

    async def _order_session_watchdog(self):
        """Re-authenticate the order session when it stops answering.

        The order path runs on its own ``ShioajiClientFacade``, built alongside
        but separately from the quote pool. The quote side has a watchdog, a
        forced-relogin path and a reconnect orchestrator; the order side was
        logged in exactly once at startup and never again. On 2026-09-08 that
        session went down and stayed down for 45 hours across zero container
        restarts, with the feed healthy the whole time -- 1,692 orders into the
        dead-letter queue and not one successful placement.

        The loop is only a clock. The decision lives in
        ``_check_order_session_once`` so it can be tested without waiting out
        an interval.
        """
        interval_s = self._env_float("HFT_ORDER_SESSION_CHECK_S", 30.0, 5.0)
        threshold = max(1, int(os.getenv("HFT_ORDER_SESSION_DOWN_THRESHOLD", "3")))
        logger.info("order_session_watchdog_started", interval_s=interval_s, threshold=threshold)
        while self.running:
            await asyncio.sleep(interval_s)
            try:
                await self._check_order_session_once(threshold)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("order_session_watchdog_error", error=str(exc))

    async def _check_order_session_once(self, threshold: int) -> str:
        """One health decision for the order session. Returns what it did.

        Driven by order outcomes, deliberately not by
        ``order_client.is_connected()``: that returns ``logged_in and api is
        not None``, and both stayed true for the whole 45-hour outage. The only
        honest signal that this session is dead is that orders keep dying on
        it::

            place_order -> SessionNotEstablished  (x threshold, in a row)
                                |
                                v
                    reconnect(force=False)   <- backoff/cooldown/login-slot
                                |                 guards live inside it
                                v
                    set_execution_callbacks()  <- MUST happen here; the
                                                  reconnect orchestrator
                                                  restores quote callbacks and
                                                  re-subscribes quotes, and
                                                  never touches the order/deal
                                                  pair. Skipping it leaves the
                                                  session authenticated and
                                                  placing orders while every
                                                  fill goes nowhere.
        """
        from hft_platform.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry.get()
        adapter = getattr(self, "order_adapter", None)
        if adapter is None:
            return "no_adapter"

        failures = int(getattr(adapter, "consecutive_session_errors", 0))
        metrics.order_session_down.set(1 if failures >= threshold else 0)
        if failures < threshold:
            return "healthy"

        reconnect = getattr(self.order_client, "reconnect", None)
        if reconnect is None:
            logger.error("order_session_down_but_client_cannot_reconnect", consecutive_session_errors=failures)
            return "cannot_reconnect"

        logger.warning("order_session_reconnect_attempt", consecutive_session_errors=failures, threshold=threshold)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(
            None,
            lambda: reconnect(reason="order_session_not_established", force=False),
        )
        if not ok:
            metrics.order_session_reconnect_total.labels(result="fail").inc()
            logger.warning("order_session_reconnect_failed_or_gated")
            return "reconnect_failed"

        callbacks = getattr(self, "_exec_callbacks", None)
        if callbacks is None:
            metrics.order_session_reconnect_total.labels(result="no_callbacks").inc()
            logger.critical("order_session_reconnected_without_execution_callbacks")
            return "no_callbacks"

        self.order_client.set_execution_callbacks(on_order=callbacks[0], on_deal=callbacks[1])
        metrics.order_session_reconnect_total.labels(result="ok").inc()
        metrics.order_session_down.set(0)
        logger.info("order_session_reconnected", previous_consecutive_errors=failures)
        return "reconnected"

    async def _pnl_snapshot_exporter(self):
        """Periodically dump position state to hft.pnl_snapshots via recorder."""
        interval_s = float(os.getenv("HFT_PNL_SNAPSHOT_INTERVAL_S", "60"))
        logger.info("PnL snapshot exporter started", interval_s=interval_s)
        while self.running:
            await asyncio.sleep(interval_s)
            try:
                now_ns = timebase.now_ns()
                total_pnl = self.position_store.total_pnl
                peak_equity = self.position_store._peak_equity_scaled
                drawdown_pct = self.position_store.get_drawdown_pct()
                positions_snap = self.position_store.snapshot_open_positions()
                for pos in positions_snap.values():
                    row = {
                        "snapshot_ts": now_ns,
                        "account_id": pos.account_id,
                        "strategy_id": pos.strategy_id,
                        "symbol": pos.symbol,
                        "net_qty": pos.net_qty,
                        "avg_price_scaled": pos.avg_price_scaled,
                        "realized_pnl_scaled": pos.realized_pnl_scaled,
                        "fees_scaled": pos.fees_scaled,
                        "total_pnl_scaled": total_pnl,
                        "peak_equity_scaled": peak_equity,
                        "drawdown_pct": drawdown_pct,
                    }
                    try:
                        self.recorder_queue.put_nowait({"topic": "pnl_snapshots", "data": row})
                    except asyncio.QueueFull:
                        self._pnl_snapshot_drops += 1
                        if self._pnl_snapshot_drops % 10 == 1:
                            logger.warning(
                                "pnl_snapshot_queue_full",
                                drops=self._pnl_snapshot_drops,
                            )
            except Exception:
                logger.warning("PnL snapshot export failed", exc_info=True)

    def _iter_supervised_services(self) -> list[tuple[str, str, Any]]:
        position_stuck_monitor = getattr(self, "position_stuck_monitor", None)
        services: list[tuple[str, str, Any]] = [
            ("md", "MarketDataService", self.md_service.run),
            ("recorder", "RecorderService", self.recorder.run),
            *(
                [("recorder_bridge", "RecorderBridge", self._recorder_bridge)]
                if not (self._md_record_direct and self._fill_record_direct and self._order_record_direct)
                else []
            ),
        ]
        if getattr(self, "order_mode", "sim") != "disabled":
            services.extend(
                (
                    ("exec_router", "ExecutionRouter", self.exec_service.run),
                    ("order", "OrderAdapter", self.order_adapter.run),
                    ("exec_gateway", "ExecutionGateway", self.execution_gateway.run),
                    ("recon", "ReconciliationService", self.recon_service.run),
                    ("strat", "StrategyRunner", self.strategy_runner.run),
                    ("pnl_exporter", "PnLSnapshotExporter", self._pnl_snapshot_exporter),
                    ("order_session_watchdog", "OrderSessionWatchdog", self._order_session_watchdog),
                )
            )
            if self.gateway_service is not None:
                services.append(("gateway", "GatewayService", self.gateway_service.run))
            else:
                services.append(("risk", "RiskEngine", self.risk_engine.run))
            if self.autonomy_monitor is not None:
                services.append(("autonomy_monitor", "AutonomyMonitor", self.autonomy_monitor.run))
            if position_stuck_monitor is not None:
                services.append(("position_stuck_monitor", "PositionStuckMonitor", position_stuck_monitor.run))
        return services

    def _reset_restart_backoff_if_healthy(self, name: str, task: asyncio.Task[Any] | None) -> None:
        if task and not task.done():
            started_at = self._task_started_at.get(name)
            if started_at is not None and (timebase.now_s() - started_at) >= self._task_healthy_uptime_s:
                self._task_restart_attempts.pop(name, None)
                self._task_restart_until_s.pop(name, None)
                self._task_started_at.pop(name, None)

    async def graceful_reset(self, *, reason: str = "operator_manual") -> dict[str, str]:
        """Reset recovery state without full system restart.

        Clears checkpoint, pending recovery positions, DLQ, HALT residue, and
        REDUCE_ONLY state. The system stays running but starts from a clean
        slate for recovery-related state.

        Returns a dict of {component: status} for operator visibility.
        """
        results: dict[str, str] = {}

        if self.checkpoint_writer is not None:
            path = getattr(self.checkpoint_writer, "_path", None)
            if path and os.path.exists(path):
                os.unlink(path)
                results["checkpoint"] = f"deleted: {path}"
                logger.info("graceful_reset: checkpoint cleared", path=path)
            else:
                results["checkpoint"] = "no file"
        else:
            results["checkpoint"] = "no writer"

        recovery = getattr(self.position_store, "_recovery_positions", None)
        if recovery is not None:
            count = len(recovery)
            recovery.clear()
            results["recovery_positions"] = f"cleared {count} entries"
            logger.info("graceful_reset: recovery positions cleared", count=count)
        else:
            results["recovery_positions"] = "no recovery state"

        try:
            from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

            dlq = get_orphaned_fill_dlq()
            dlq_count = dlq.count
            dlq.drain()
            results["fill_dlq"] = f"drained {dlq_count} entries"
            logger.info("graceful_reset: fill DLQ drained", count=dlq_count)
        except Exception as exc:
            results["fill_dlq"] = f"error: {exc}"

        if self.storm_guard.state >= StormGuardState.STORM:
            old_state = self.storm_guard.state.name
            self.storm_guard._halt_entry_ts = 0.0
            self.storm_guard._storm_entry_ts = 0.0
            self.storm_guard._de_escalate_count = self.storm_guard._de_escalate_threshold
            self.storm_guard.update(latency_us=0, feed_gap_s=0.0)
            results["storm_guard"] = f"reset from {old_state} to {self.storm_guard.state.name}"
            logger.info("graceful_reset: storm_guard reset", old=old_state, new=self.storm_guard.state.name)
        else:
            results["storm_guard"] = "already NORMAL"

        if self.platform_degrade_controller.reduce_only_active:
            await self.platform_degrade_controller.exit_reduce_only_async(reason=reason)
            results["reduce_only"] = "exited"
            logger.info("graceful_reset: REDUCE_ONLY exited", reason=reason)
        else:
            results["reduce_only"] = "not active"

        if self.recon_service is not None:
            self.recon_service._halt_triggered = False
            self.recon_service._consecutive_failures = 0
            self.recon_service._broker_zero_streak = 0
            self.recon_service._noncritical_drift_streak = 0
            results["reconciliation"] = "state reset"
            logger.info("graceful_reset: reconciliation state reset")
        else:
            results["reconciliation"] = "no service"

        logger.warning("graceful_reset_completed", reason=reason, results=results)
        return results

    def _try_restart_service(
        self,
        name: str,
        component: str,
        coro_factory: Any,
        *,
        count_attempt: bool = True,
    ) -> None:
        """Restart a supervised service task.

        Args:
            count_attempt: whether this restart consumes the crash-recovery
                budget (``HFT_TASK_RESTART_MAX_ATTEMPTS``). ``True`` for a task
                that died; ``False`` for a controlled restart of a task that
                exited cleanly because the supervisor stopped it. The budget
                exists to stop a crash loop, and spending it on normal control
                flow means a real crash later has no restarts left. Production
                2026-08-21: a HALT that oscillated 25 times drove ``order`` to
                ``attempt=10 max_attempts=10`` and then latched a HALT reading
                "Service order crash-loop: 10 restarts exceeded max" -- with
                nothing having crashed.
        """
        now_s = timebase.now_s()
        allowed_at = self._task_restart_until_s.get(name, 0.0)
        if now_s < allowed_at:
            return
        if not count_attempt:
            logger.info(
                "Restarting service task (uncounted)",
                task=name,
                component=component,
                reason="controlled_restart",
            )
            self._start_service(name, coro_factory())
            self._task_started_at[name] = timebase.now_s()
            return
        attempt = self._task_restart_attempts.get(name, 0) + 1

        if attempt > self._task_restart_max_attempts:
            logger.critical(
                "Service exceeded max restart attempts — permanently stopped",
                task=name,
                component=component,
                attempts=attempt - 1,
                max_attempts=self._task_restart_max_attempts,
            )
            sg = getattr(self, "storm_guard", None)
            if sg is not None:
                sg.trigger_halt(f"Service {name} crash-loop: {attempt - 1} restarts exceeded max")
            return

        delay_s = min(self._task_restart_base_delay_s * (2 ** (attempt - 1)), self._task_restart_max_delay_s)
        self._task_restart_attempts[name] = attempt
        self._task_restart_until_s[name] = now_s + delay_s
        logger.warning(
            "Restarting service task",
            task=name,
            component=component,
            attempt=attempt,
            max_attempts=self._task_restart_max_attempts,
            next_retry_after_s=round(delay_s, 2),
        )
        self._start_service(name, coro_factory())
        self._task_started_at[name] = timebase.now_s()

    async def _update_platform_degrade_state(self) -> None:
        """One supervisor tick of the control plane.

        Async because everything under it that touches the safety document does
        so through a shared ``flock``: the decisions stay on the loop, the reads
        and writes do not. See ``StrategyHealthGovernor.quarantine_async``.
        """
        state = await asyncio.to_thread(self._read_rearm_state)
        controller = getattr(self, "platform_degrade_controller", None)
        inputs = getattr(self, "platform_degrade_inputs", None)
        reasons = inputs.reduce_only_reasons() if inputs is not None else []
        await self._consume_platform_rearm_request_async(state, reasons)
        await self._consume_strategy_rearm_requests(state)
        await self._consume_strategy_quarantine_requests()
        if controller is None or inputs is None:
            return
        for reason in reasons:
            await controller.enter_reduce_only_async(reason=reason)
        await controller.check_auto_recovery_async(
            current_reasons=reasons,
            now_ns=timebase.now_ns(),
        )

    def _read_rearm_state(self) -> dict | None:
        """Load runtime_state.json once per tick for both re-arm consumers.

        Split out so adding the strategy-scope consumer below does not add a
        second file read to every supervisor tick.
        """
        gate = getattr(self, "manual_rearm_gate", None)
        if gate is None:
            return None
        try:
            return gate.snapshot()
        except Exception as exc:
            logger.warning("manual_rearm_state_read_failed", error=str(exc))
            return None

    def _retire_rearm_request(self, request: Any) -> bool:
        """Delete one request, reporting failure instead of raising.

        ``consume`` is one ``unlink``, but a read-only remount, a permission
        change or a transient filesystem error makes it throw -- and nothing
        between here and ``HFTSystem.run()`` catches it, so the supervisor would
        die. The request survives the restart, so the next boot dies the same
        way: a crash loop out of a control-plane housekeeping step.

        Returning False leaves the quarantine latched and the request in place
        for the next tick, which is the fail-closed direction.
        """
        try:
            rearm_requests.consume(request)
        except Exception as exc:
            logger.error(
                "strategy_rearm_request_delete_failed",
                strategy_id=getattr(request, "strategy_id", None),
                request_id=getattr(request, "request_id", None),
                error=str(exc),
            )
            return False
        return True

    def _retire_quarantine_request(self, request: Any) -> bool:
        """Delete one quarantine request, reporting failure instead of raising.

        Same contract as :meth:`_retire_rearm_request`: an ``unlink`` that
        throws must not kill the supervisor, because the request survives the
        restart and the next boot would die the same way.

        The fail-closed direction differs, though, and that is why this is not
        shared with the re-arm helper. There, a failed delete leaves the
        strategy quarantined. Here it leaves the request on disk to be applied
        again next tick -- which is harmless, because quarantining an already
        quarantined strategy is a no-op.
        """
        try:
            quarantine_requests.consume(request)
        except Exception as exc:
            logger.error(
                "strategy_quarantine_request_delete_failed",
                strategy_id=getattr(request, "strategy_id", None),
                request_id=getattr(request, "request_id", None),
                error=str(exc),
            )
            return False
        return True

    async def _consume_strategy_quarantine_requests(self) -> None:
        """Apply operator quarantine requests to the live governor.

        The inverse of :meth:`_consume_strategy_rearm_requests`, and it did not
        exist. ``StrategyHealthGovernor.quarantine_async`` had no
        operator-reachable caller, and the config lever an operator would reach
        for instead -- ``enabled: false`` in ``config/live/strategies.yaml`` --
        makes the engine refuse to start while the loop binds that strategy
        (``config/loader.py:_assert_strategy_enabled``). Measured 2026-09-03:
        that attempt crash-looped the engine, RestartCount 0 -> 10. Stopping the
        platform's only strategy required stopping the engine.

        Unlike a re-arm, a quarantine request carries no token -- it creates the
        latch rather than releasing a named one -- so staleness is bounded by a
        TTL instead. An expired request is retired unapplied and logged.
        """
        runner = getattr(self, "strategy_runner", None)
        governor = getattr(runner, "strategy_governor", None)
        if governor is None:
            return
        gate = getattr(self, "manual_rearm_gate", None)
        if gate is None:
            return
        try:
            requests = await asyncio.to_thread(quarantine_requests.pending, gate.state_path.parent)
        except Exception as exc:
            logger.warning("strategy_quarantine_request_scan_failed", error=str(exc))
            return
        for request in requests:
            if quarantine_requests.is_expired(request):
                if not await asyncio.to_thread(self._retire_quarantine_request, request):
                    continue
                logger.warning(
                    "strategy_quarantine_request_expired",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                    requested_at_ns=request.requested_at_ns,
                    consequence="retired unapplied; reissue if the strategy should still stop",
                )
                continue
            if governor.quarantine_token(request.strategy_id) is not None:
                if not await asyncio.to_thread(self._retire_quarantine_request, request):
                    continue
                logger.warning(
                    "strategy_quarantine_request_already_quarantined",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                )
                continue
            try:
                await governor.quarantine_async(request.strategy_id, reason=request.reason)
            except Exception as exc:
                logger.error(
                    "strategy_quarantine_request_failed",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                    error=str(exc),
                )
                continue
            await asyncio.to_thread(self._retire_quarantine_request, request)
            logger.warning(
                "strategy_quarantine_applied_from_operator_request",
                strategy_id=request.strategy_id,
                request_id=request.request_id,
                reason=request.reason,
            )

    async def _consume_strategy_rearm_requests(self, state: dict | None = None) -> None:
        """Apply operator re-arm requests to the live governor.

        Without this the strategy re-arm is a **write-only loop**. The quarantine
        that gates dispatch is ``StrategyHealthGovernor._quarantined``, an
        in-memory dict; the CLI only wrote to disk, and nothing read it back --
        ``StrategyHealthGovernor.rearm`` had no production caller at all. The
        command reported success and changed nothing, leaving an engine restart
        as the only real remedy. Measured on THESHOW: ``R47_MAKER_TMF`` was
        quarantined at 2026-08-23T14:18:20Z by a single rejected intent and
        emitted no alpha decision for 46 h, paging the whole time.

        Requests arrive as write-once files, each naming the ``quarantine_token``
        it authorizes. This runs entirely on the event loop and entirely in
        memory: listing an empty directory is one ``scandir``, and matching a
        token against the live dict is microseconds. Nothing here needs a worker,
        a deadline, a lock, or a consumed-id watermark -- a request is consumed
        by deleting it, so a replay is impossible because the request is gone.

        ``state`` is the supervisor's single shared read of the safety document.
        It is used to converge on latches another engine may have persisted
        after this one booted, before any request is applied.
        """
        runner = getattr(self, "strategy_runner", None)
        governor = getattr(runner, "strategy_governor", None)
        if governor is None:
            return
        gate = getattr(self, "manual_rearm_gate", None)
        if gate is None:
            return
        if state:
            try:
                governor.reconcile_persisted_quarantines(state)
            except Exception as exc:
                logger.warning("strategy_quarantine_reconcile_failed", error=str(exc))
        try:
            requests = await asyncio.to_thread(rearm_requests.pending, gate.state_path.parent)
        except Exception as exc:
            logger.warning("strategy_rearm_request_scan_failed", error=str(exc))
            return
        for request in requests:
            if not governor.owns_token(request.quarantine_token):
                seen = getattr(self, "_foreign_rearm_request_ids", None)
                if seen is None:
                    seen = self._foreign_rearm_request_ids = set()
                if request.request_id not in seen:
                    seen.add(request.request_id)
                    logger.warning(
                        "strategy_rearm_request_foreign_token",
                        strategy_id=request.strategy_id,
                        request_id=request.request_id,
                        authorized_token=request.quarantine_token,
                        consequence="left in place for the engine that minted the token",
                    )
                continue
            live_token = governor.quarantine_token(request.strategy_id)
            if live_token is None:
                if not await asyncio.to_thread(self._retire_rearm_request, request):
                    continue
                logger.warning(
                    "strategy_rearm_request_retired_no_live_quarantine",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                )
                continue
            if live_token != request.quarantine_token:
                if not await asyncio.to_thread(self._retire_rearm_request, request):
                    continue
                logger.warning(
                    "strategy_rearm_request_superseded",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                    authorized_token=request.quarantine_token,
                    live_token=live_token,
                )
                continue
            if not await asyncio.to_thread(self._retire_rearm_request, request):
                continue
            if await governor.rearm_async(
                request.strategy_id,
                expected_token=request.quarantine_token,
                request_id=request.request_id,
            ):
                logger.warning(
                    "strategy_rearm_applied_from_operator_request",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                    quarantine_token=request.quarantine_token,
                )

    def _platform_rearm_is_due(self, state: Any) -> bool:
        """Whether this tick should force-clear platform reduce-only.

        Shared by the sync and async consumers so the request-sequence bookkeeping
        (``_last_platform_rearm_request_seen``) cannot drift between them: an
        operator request must be consumed exactly once whichever path sees it.
        """
        gate = getattr(self, "manual_rearm_gate", None)
        controller = getattr(self, "platform_degrade_controller", None)
        if gate is None or controller is None:
            return False
        if not state:
            return False

        platform = state.get("platform")
        if not isinstance(platform, dict):
            return False
        try:
            requested_at = float(platform.get("rearm_requested_at") or 0.0)
        except (TypeError, ValueError):
            requested_at = 0.0

        last_seen = float(getattr(self, "_last_platform_rearm_request_seen", 0.0))
        if requested_at <= last_seen:
            return False
        self._last_platform_rearm_request_seen = requested_at

        if bool(platform.get("manual_rearm_required")):
            return False
        return bool(getattr(controller, "reduce_only_active", False))

    def _consume_platform_rearm_request(self, state: Any = _STATE_NOT_SUPPLIED) -> None:
        """Synchronous consumer, for callers that are not on the event loop."""
        if state is _STATE_NOT_SUPPLIED:
            state = self._read_rearm_state()
        if self._platform_rearm_is_due(state):
            self.platform_degrade_controller.force_clear(reason="manual_rearm_gate")

    async def _consume_platform_rearm_request_async(
        self,
        state: Any = _STATE_NOT_SUPPLIED,
        current_reasons: "list[str] | tuple[str, ...]" = (),
    ) -> None:
        """The supervisor's consumer: decide on the loop, persist off it.

        ``force_clear`` reaches ``exit_reduce_only`` and ``record_transition``,
        which take the shared ``flock`` (polled with ``time.sleep``, two-second
        deadline) and fsync twice. This was the one un-awaited call left on
        ``_update_platform_degrade_state``, and it sat on the manual path -- the
        stall landed exactly when an operator was restoring permissions.
        """
        if state is _STATE_NOT_SUPPLIED:
            state = await asyncio.to_thread(self._read_rearm_state)
        if self._platform_rearm_is_due(state):
            await self.platform_degrade_controller.force_clear_async(
                reason="manual_rearm_gate", current_reasons=current_reasons
            )

    async def _supervise(self):
        """
        Active Supervisor Loop.
        1. Monitors StormGuard triggers (Latency, Gaps).
        2. Monitors Service Health (Crashes).
        """
        from hft_platform.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry.get()

        loop = asyncio.get_running_loop()
        interval_s = 1.0
        last_tick = loop.time()
        _heartbeat_path = os.getenv("HFT_HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH)
        _heartbeat_interval_ticks = int(os.getenv("HFT_HEARTBEAT_INTERVAL_S", "30"))
        _heartbeat_tick = 0

        _hb_ok, _hb_reason = heartbeat_writable(_heartbeat_path)
        if not _hb_ok:
            logger.critical(
                "heartbeat_path_not_writable",
                path=_heartbeat_path,
                reason=_hb_reason,
                hint=(
                    "external heartbeat watchdog is disabled; chown the heartbeat dir "
                    "to the container uid or set HFT_HEARTBEAT_PATH to a writable path"
                ),
            )

        _gc_gen0_interval = max(1, int(os.getenv("HFT_GC_GEN0_INTERVAL_TICKS", "10")))
        _gc_gen0_tick = 0
        _gc_gen0_enabled = os.getenv("HFT_GC_DISABLE_TRADING", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        } and os.getenv("HFT_GC_GEN0_PERIODIC", "1").strip().lower() not in {"0", "false", "no", "off"}
        _gc_gen2_interval = max(60, int(os.getenv("HFT_GC_GEN2_INTERVAL_TICKS", "3600")))
        _gc_gen2_tick = 0
        _gc_gen2_enabled = _gc_gen0_enabled

        while self.running:
            await asyncio.sleep(interval_s)
            now_tick = loop.time()
            lag_s = max(0.0, now_tick - last_tick - interval_s)
            metrics.event_loop_lag_ms.set(lag_s * 1000.0)
            last_tick = now_tick

            _watchdog = getattr(self, "_loop_watchdog", None)
            if _watchdog is not None:
                _watchdog.beat()

            feed_gap_s = 0.0
            try:
                feed_gap_s = self._get_max_feed_gap_s(self.md_service)
            except Exception as e:
                logger.warning("StormGuard feed_gap computation failed", error=str(e))

            if self._mtm_calculator is not None:
                try:
                    mtm = self._mtm_calculator.snapshot()
                    self._log_mtm_completeness(mtm)
                    self.risk_engine.update_unrealized_pnl(int(mtm.total_scaled), complete=mtm.complete)
                except Exception as e:
                    logger.warning("daily-loss mark-to-market update failed", error=str(e))

            try:
                self.risk_engine.roll_daily_loss_boundary()
            except Exception as e:
                logger.warning("daily loss boundary roll failed", error=str(e))

            try:
                await self._sync_manual_latches()
            except Exception as e:
                logger.error("manual latch sync failed", error=str(e), exc_info=True)

            try:
                await self._sync_operator_flatten()
            except Exception as e:
                logger.error("operator flatten sync failed", error=str(e), exc_info=True)
            try:
                self._run_loss_exit()
            except Exception as e:
                logger.error("loss exit failed", error=str(e), exc_info=True)

            latency_us = self._stormguard_latency_us(lag_s)

            try:
                if self.session_governor is not None:
                    from hft_platform.ops.session_governor import SessionPhase

                    _ACTIVE_PHASES = frozenset(
                        {
                            SessionPhase.PRE_OPEN,
                            SessionPhase.OPEN,
                            SessionPhase.CLOSE_ONLY,
                            SessionPhase.FORCE_FLAT,
                        }
                    )
                    gate = getattr(self.session_governor, "track_gate", None)
                    if gate is not None:
                        phases = gate.track_phases
                        any_open = any(p in _ACTIVE_PHASES for p in phases.values())
                        self.storm_guard.set_session_active(any_open)
            except Exception as e:
                logger.warning("StormGuard session state update failed", error=str(e))

            try:
                self.storm_guard.update(
                    latency_us=latency_us,
                    feed_gap_s=feed_gap_s,
                )
            except Exception as e:
                logger.warning("StormGuard update call failed", error=str(e))

            try:
                if hasattr(self.storm_guard, "update_with_lob"):
                    lob_engine = getattr(self.md_service, "lob", None)
                    if lob_engine is not None:
                        book = self._drift_burst_book(lob_engine)
                        if book is not None:
                            self.storm_guard.update_with_lob(
                                mid_price_x2=book.mid_price_x2,
                                spread_scaled=book.spread,
                                imbalance=book.imbalance,
                                ts=timebase.now_ns(),
                                symbol=getattr(self, "_drift_burst_symbol", ""),
                            )
            except Exception as e:
                logger.warning("StormGuard LOB drift-burst update failed", error=str(e))

            try:
                feed_gap_metric = getattr(metrics, "feed_gap_by_symbol_seconds", None)
                if feed_gap_metric is not None:
                    for symbol, gap in self._get_feed_gaps_by_symbol(self.md_service).items():
                        capped = metrics.cap_symbol(symbol) if metrics else symbol
                        feed_gap_metric.labels(symbol=capped).set(gap)
            except Exception as e:
                logger.warning("StormGuard per-symbol metrics failed", error=str(e))

            _update_pool_metrics = getattr(self.md_client, "update_metrics", None)
            if _update_pool_metrics is not None:
                try:
                    _update_pool_metrics()
                except Exception as e:
                    logger.warning("pool_metrics_update_failed", error=str(e))

            loop = asyncio.get_running_loop()
            kill_switch_path = await loop.run_in_executor(None, kill_switch.active_path)
            if kill_switch_path is not None:
                if self.storm_guard.state != StormGuardState.HALT:
                    try:
                        _ks_reason = await loop.run_in_executor(None, _read_kill_switch_reason, kill_switch_path)
                    except Exception:
                        _ks_reason = "kill_switch_file_present"
                    self.storm_guard.trigger_halt(f"KILL_SWITCH_FILE: {_ks_reason}")
                    logger.critical("Kill switch file detected", path=kill_switch_path, reason=_ks_reason)
                self.storm_guard.set_kill_switch_hold(True)
                self._kill_switch_actor = await loop.run_in_executor(None, _read_kill_switch_actor, kill_switch_path)
            elif not self._recovery_halted:
                self.storm_guard.set_kill_switch_hold(False)
                self._kill_switch_actor = ""
            else:
                self._kill_switch_actor = ""
                self.storm_guard.set_kill_switch_hold(True)

            t_gateway = self.tasks.get("exec_gateway")
            for name, component, coro_factory in self._iter_supervised_services():
                task = self.tasks.get(name)
                if task is None:
                    continue
                self._reset_restart_backoff_if_healthy(name, task)
                if not task.done():
                    continue
                try:
                    exc = task.exception()
                except asyncio.CancelledError:
                    continue
                if exc is None and not self.running:
                    continue
                if exc is None and name in ("order", "exec_gateway") and self.storm_guard.state == StormGuardState.HALT:
                    continue
                if exc is None and name in ("order", "exec_gateway") and self.storm_guard.state != StormGuardState.HALT:
                    logger.info("Restarting service after HALT de-escalation", task=name)
                    _svc = self.order_adapter if name == "order" else self.execution_gateway
                    self._set_service_running(_svc, True)
                    if self.running:
                        self._try_restart_service(name, component, coro_factory, count_attempt=False)
                    continue
                _started = self._task_started_at.get(name)
                _immediate_fail = _started is not None and (timebase.now_s() - _started) < 2.0
                if _immediate_fail:
                    cur = self._task_restart_attempts.get(name, 0)
                    self._task_restart_attempts[name] = cur + 1
                logger.critical(
                    "Critical service task stopped",
                    task=name,
                    component=component,
                    error=str(exc) if exc else "task_exited_without_exception",
                    immediate_fail=_immediate_fail,
                )
                self.storm_guard.trigger_halt(f"Critical Component Crash: {component}")
                if self.running:
                    self._try_restart_service(name, component, coro_factory)

            await loop.run_in_executor(None, metrics.update_system_metrics)
            client = getattr(self.md_service, "client", None)
            if client is not None and hasattr(client, "check_facade_health"):
                client.check_facade_health()
            if metrics:
                exec_task = self.tasks.get("exec_router")
                gateway_task = self.tasks.get("exec_gateway")
                metrics.execution_router_alive.set(1 if exec_task and not exec_task.done() else 0)
                metrics.execution_gateway_alive.set(1 if gateway_task and not gateway_task.done() else 0)
                metrics.queue_depth.labels(queue="raw").set(self.raw_queue.qsize())
                metrics.queue_depth.labels(queue="raw_exec").set(self.raw_exec_queue.qsize())
                metrics.queue_depth.labels(queue="recorder").set(self.recorder_queue.qsize())
                metrics.queue_depth.labels(queue="risk").set(self.risk_queue.qsize())
                metrics.queue_depth.labels(queue="order").set(self.order_queue.qsize())
                if self.intent_channel is not None:
                    depth = getattr(self.intent_channel, "qsize", lambda: 0)()
                    metrics.queue_depth.labels(queue="gateway_intent").set(depth)
                _api_q = getattr(self.order_adapter, "_api_queue", None)
                if _api_q is not None:
                    metrics.queue_depth.labels(queue="gateway_api").set(_api_q.qsize())
            now_s = timebase.now_s()
            if now_s - self._last_queue_log_s >= self._queue_log_every_s:
                self._last_queue_log_s = now_s
                _gateway_intent_depth = (
                    getattr(self.intent_channel, "qsize", lambda: 0)() if self.intent_channel is not None else None
                )
                _log_kwargs: dict = dict(
                    raw=self.raw_queue.qsize(),
                    rec=self.recorder_queue.qsize(),
                    risk=self.risk_queue.qsize(),
                    order=self.order_queue.qsize(),
                    raw_exec=self.raw_exec_queue.qsize(),
                )
                if _gateway_intent_depth is not None:
                    _log_kwargs["gateway_intent"] = _gateway_intent_depth
                _api_q_log = getattr(self.order_adapter, "_api_queue", None)
                if _api_q_log is not None:
                    _log_kwargs["gateway_api"] = _api_q_log.qsize()
                logger.info("Queues", **_log_kwargs)

            await self._update_platform_degrade_state()

            _fe = getattr(getattr(self, "md_service", None), "feature_engine", None)
            if _fe is not None:
                try:
                    _fe.evict_stale_symbols()
                except Exception:
                    pass

            _oa = getattr(self, "order_adapter", None)
            if _oa is not None:
                try:
                    await _oa.sweep_stale_live_orders()
                except Exception:
                    pass
                try:
                    await _oa.release_stale_phantom_pendings()
                except Exception:
                    pass

            now = timebase.now_s()
            t_router = self.tasks.get("exec_router")
            if t_router and not t_router.done():
                metrics.execution_router_heartbeat_ts.set(now)
            if t_gateway and not t_gateway.done():
                metrics.execution_gateway_heartbeat_ts.set(now)

            _heartbeat_tick += 1
            if _heartbeat_tick >= _heartbeat_interval_ticks:
                _heartbeat_tick = 0
                write_heartbeat(_heartbeat_path)

            if self.storm_guard.state == StormGuardState.HALT:
                _now_mono = time.monotonic()
                _halt_started_mono = self._halt_episode_start_mono
                _halt_log_kind = self._halt_status_log_kind(_now_mono)
                if _halt_log_kind == "entry":
                    logger.error(
                        "System HALTED by StormGuard - blocking orders",
                        reason=self.storm_guard.state_reason,
                    )
                elif _halt_log_kind == "heartbeat" and _halt_started_mono is not None:
                    logger.warning(
                        "System still HALTED by StormGuard - blocking orders",
                        reason=self.storm_guard.state_reason,
                        halted_for_s=round(_now_mono - _halt_started_mono, 1),
                    )
                if self.gateway_service is not None:
                    self.gateway_service.set_halt()
                self._write_halt_entry_checkpoint()
                risk_drained = 0
                _requeue: list = []
                while not self.risk_queue.empty():
                    try:
                        item = self.risk_queue.get_nowait()
                        self.risk_queue.task_done()
                        _itype = getattr(item, "intent_type", None)
                        _sid = getattr(item, "strategy_id", None)
                        _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                        _is_exempt = bool(_sid) and self.storm_guard.is_halt_exempt(_sid)
                        if _is_safety or _is_exempt:
                            _requeue.append(item)
                        else:
                            risk_drained += 1
                            self._release_halt_drained(item)
                    except asyncio.QueueEmpty:
                        break
                for item in _requeue:
                    try:
                        self.risk_queue.put_nowait(item)
                    except asyncio.QueueFull:
                        logger.critical(
                            "risk_queue_full_safety_intent_lost",
                            strategy_id=getattr(item, "strategy_id", "?"),
                            intent_type=str(getattr(item, "intent_type", "?")),
                        )
                        try:
                            from hft_platform.observability.metrics import MetricsRegistry

                            MetricsRegistry.get().halt_drain_safety_intent_lost_total.inc()
                        except Exception as exc:
                            logger.warning("halt_drain_metric_inc_failed", error=str(exc))
                if risk_drained > 0:
                    logger.warning("Drained blocked intents from risk_queue during HALT", count=risk_drained)
                if self.intent_channel is not None and hasattr(self.intent_channel, "drain_nowait"):
                    _ic_drained = 0
                    _ic_requeue: list = []
                    _all_envelopes = self.intent_channel.drain_nowait()
                    for envelope in _all_envelopes:
                        _itype = self.intent_channel.envelope_intent_type(envelope)
                        _sid = self.intent_channel.envelope_strategy_id(envelope)
                        _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                        _is_exempt = bool(_sid) and self.storm_guard.is_halt_exempt(_sid)
                        if _is_safety or _is_exempt:
                            _ic_requeue.append(envelope)
                        else:
                            _ic_drained += 1
                            self._release_halt_drained(envelope)
                    for envelope in _ic_requeue:
                        try:
                            self.intent_channel._queue.put_nowait(envelope)
                        except asyncio.QueueFull:
                            logger.critical(
                                "intent_channel_full_safety_intent_lost",
                                strategy_id=self.intent_channel.envelope_strategy_id(envelope),
                            )
                    if _ic_drained > 0:
                        logger.warning(
                            "Drained blocked intents from intent_channel during HALT",
                            count=_ic_drained,
                        )
                drained_count = 0
                _cmd_requeue: list = []
                while not self.order_queue.empty():
                    try:
                        cmd = self.order_queue.get_nowait()
                        self.order_queue.task_done()
                        _intent = getattr(cmd, "intent", None)
                        _itype = getattr(_intent, "intent_type", None) if _intent else None
                        _sid = getattr(_intent, "strategy_id", None) if _intent else None
                        _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                        _is_exempt = bool(_sid) and self.storm_guard.is_halt_exempt(_sid)
                        if _is_safety or _is_exempt:
                            _cmd_requeue.append(cmd)
                        else:
                            drained_count += 1
                            self._release_halt_drained(cmd)
                    except asyncio.QueueEmpty:
                        break
                for cmd in _cmd_requeue:
                    try:
                        _task = asyncio.create_task(self.order_adapter.execute(cmd))
                        _task.add_done_callback(_log_safety_dispatch_error)
                        _bg = getattr(self.order_adapter, "_background_tasks", None)
                        if _bg is not None:
                            _bg.add(_task)
                            _task.add_done_callback(_bg.discard)
                        logger.info(
                            "halt_drain_safety_cmd_dispatched",
                            cmd_id=getattr(cmd, "cmd_id", "?"),
                            intent_type=str(getattr(getattr(cmd, "intent", None), "intent_type", "?")),
                        )
                    except Exception as exc:
                        logger.critical(
                            "halt_drain_safety_cmd_dispatch_failed",
                            cmd_id=getattr(cmd, "cmd_id", "?"),
                            error=str(exc),
                        )
                if drained_count > 0:
                    logger.warning("Drained blocked orders during HALT", count=drained_count)
                self._set_service_running(self.order_adapter, False)
                try:
                    _drain_task = asyncio.create_task(self.order_adapter.drain_and_cancel())
                    _bg = getattr(self.order_adapter, "_background_tasks", None)
                    if _bg is not None:
                        _bg.add(_drain_task)
                        _drain_task.add_done_callback(_bg.discard)
                except Exception as exc:
                    logger.warning("In-flight order cancellation failed during HALT", error=str(exc))
            else:
                if self.gateway_service is not None:
                    self.gateway_service.set_normal()
                self._set_service_running(self.order_adapter, True)
                self._halt_checkpoint_written = False
                self._halt_episode_start_mono = None

            if _gc_gen0_enabled:
                _gc_gen0_tick += 1
                if _gc_gen0_tick >= _gc_gen0_interval:
                    _gc_gen0_tick = 0
                    gc.collect(0)
            if _gc_gen2_enabled:
                _gc_gen2_tick += 1
                if _gc_gen2_tick >= _gc_gen2_interval:
                    _gc_gen2_tick = 0
                    _gc2_start = loop.time()
                    _gc2_collected = gc.collect(2)
                    _gc2_ms = (loop.time() - _gc2_start) * 1000.0
                    if _gc2_ms > 5.0 or _gc2_collected > 100:
                        logger.info(
                            "gc_gen2_periodic",
                            collected=_gc2_collected,
                            duration_ms=round(_gc2_ms, 2),
                        )

            _tick_hist = getattr(metrics, "supervisor_tick_duration_ms", None)
            if _tick_hist is not None:
                _tick_hist.observe((loop.time() - now_tick) * 1000.0)

    def _stormguard_latency_us(self, lag_s: float) -> int:
        """Return the loop-congestion value StormGuard should evaluate.

        This used to be ``lag_s * 1e6`` -- ``event_loop_lag_ms``, which spans
        the supervisor's whole tick period and therefore includes this loop's
        own work. Measured on THESHOW over 22 h (2026-08-22) the supervisor's
        body was **89.3%** of that composite (``supervisor_tick_duration_ms``
        mean 4.93 ms against ``event_loop_probe_lag_ms`` mean 0.59 ms), so the
        breaker was armed on a number that mostly measured the supervisor
        timing itself.

        The idle probe reports only time the loop was unavailable to *any*
        callback, which is what the breaker is about. Peak rather than mean,
        because one blocked interval is the event of interest, and drained so a
        spike is evaluated exactly once instead of holding the breaker up after
        the loop has recovered.

        Falls back to the old composite while the probe has produced no samples
        at all -- not started, or disabled. A risk breaker must not be handed a
        confident zero by an input that simply is not running.
        """
        samples = self._loop_probe_samples
        self._loop_probe_samples = 0
        if samples > 0:
            peak_ms = self._loop_probe_peak_ms
            self._loop_probe_peak_ms = 0.0
            return int(peak_ms * 1_000.0)
        return int(lag_s * 1_000_000)

    async def _probe_event_loop_lag(self):
        """Measure event-loop congestion with a task that does nothing else.

        The supervisor's ``event_loop_lag_ms`` cannot separate "the loop was
        busy" from "this tick did a lot of work", because it times its own
        period. This probe sleeps for a fixed short interval and records the
        overshoot, so every millisecond it reports is time the loop was
        unavailable to *any* callback — including the hot path.
        """
        from hft_platform.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry.get()
        hist = getattr(metrics, "event_loop_probe_lag_ms", None)
        loop = asyncio.get_running_loop()
        interval_s = self._env_float("HFT_LOOP_PROBE_INTERVAL_S", 0.1, 0.01)
        last = loop.time()
        while self.running:
            await asyncio.sleep(interval_s)
            now = loop.time()
            overshoot_ms = max(0.0, (now - last - interval_s) * 1000.0)
            last = now
            if hist is not None:
                hist.observe(overshoot_ms)
            if overshoot_ms > self._loop_probe_peak_ms:
                self._loop_probe_peak_ms = overshoot_ms
            self._loop_probe_samples += 1

    async def stop_async(self):
        """Async stop with proper task cleanup."""
        self.running = False
        _watchdog = getattr(self, "_loop_watchdog", None)
        if _watchdog is not None:
            _watchdog.stop()
        self.md_service.running = False
        self.exec_service.running = False
        self.risk_engine.running = False
        self.recon_service.running = False

        _drain_timeout_ms = int(os.getenv("HFT_BUS_DRAIN_TIMEOUT_MS", "500"))
        _drain_timeout_s = _drain_timeout_ms / 1000.0
        _bus = getattr(self, "bus", None)
        _sr = getattr(self, "strategy_runner", None)
        if _bus is not None and _sr is not None and hasattr(_sr, "drain_to_cursor"):
            _target_cursor = getattr(_bus, "cursor", -1)
            if _target_cursor >= 0:
                try:
                    _drained, _skipped = await asyncio.wait_for(
                        _sr.drain_to_cursor(_target_cursor, _drain_timeout_s),
                        timeout=_drain_timeout_s + 0.1,
                    )
                    if _skipped > 0:
                        logger.warning(
                            "Bus drain timeout: events skipped",
                            drained=_drained,
                            skipped=_skipped,
                            timeout_ms=_drain_timeout_ms,
                        )
                    else:
                        logger.info(
                            "Bus drain complete",
                            drained=_drained,
                            timeout_ms=_drain_timeout_ms,
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Bus drain outer timeout during shutdown",
                        timeout_ms=_drain_timeout_ms,
                    )
                except Exception as exc:
                    logger.warning("Bus drain failed during shutdown", error=str(exc))

        self.strategy_runner.running = False
        self.execution_gateway.stop()
        self.session_hook_manager.stop()
        self.health_server.stop()

        if getattr(self, "gateway_service", None) is not None:
            self.gateway_service.running = False

        try:
            drained = await asyncio.wait_for(self.exec_service.stop(), timeout=3.0)
            if drained:
                logger.info("ExecutionRouter shutdown drain", fills_drained=drained)
        except asyncio.TimeoutError:
            logger.warning("ExecutionRouter drain timeout during shutdown")
        except Exception as exc:
            logger.warning("ExecutionRouter drain failed", error=str(exc))

        try:
            await self.exec_service.flush_fill_dedup()
            self.exec_service.persist_fill_dedup()
        except Exception as exc:
            logger.warning("fill_dedup_persist_failed_shutdown", error=str(exc))

        try:
            from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

            _fill_dlq = get_orphaned_fill_dlq()
            _fill_dlq.persist()
            if _fill_dlq.last_unrecovered:
                logger.critical("fill_dlq_fills_without_durable_copy_at_shutdown", count=_fill_dlq.last_unrecovered)
        except Exception as exc:
            logger.warning("fill_dlq_persist_failed_shutdown", error=str(exc))

        try:
            await asyncio.wait_for(self.order_adapter.drain_and_cancel(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Order drain timeout during shutdown")
        except Exception as exc:
            logger.warning("Order drain failed during shutdown", error=str(exc))

        try:
            await self.order_adapter.flush_order_id_map()
            self.order_adapter.persist_order_id_map()
        except Exception as exc:
            logger.warning("order_id_map_persist_failed_shutdown", error=str(exc))

        self._write_final_checkpoint()

        _aw = getattr(self, "_audit_writer", None)
        if _aw is not None:
            try:
                await _aw.stop()
                logger.info("AuditWriter stopped")
            except Exception as exc:
                logger.warning("AuditWriter stop failed", error=str(exc))

        for cn in ("md_client", "order_client"):
            self._close_broker_client(cn)

        bridge_task = self.tasks.get("recorder_bridge")
        if bridge_task and not bridge_task.done():
            bridge_task.cancel()
            try:
                await asyncio.wait_for(bridge_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.error("recorder_bridge cleanup error", error=str(e))

        if hasattr(self, "recorder") and self.recorder is not None:
            self.recorder.running = False

        recorder_task = self.tasks.get("recorder")
        if recorder_task and not recorder_task.done():
            _recorder_timeout = float(os.getenv("HFT_RECORDER_SHUTDOWN_TIMEOUT_S", "60")) + 5.0
            try:
                await asyncio.wait_for(recorder_task, timeout=_recorder_timeout)
                logger.info("Recorder shutdown complete")
            except asyncio.TimeoutError:
                logger.warning("Recorder shutdown timeout, cancelling", timeout_s=_recorder_timeout)
                recorder_task.cancel()
                try:
                    await asyncio.wait_for(recorder_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Recorder shutdown error", error=str(exc))

        for name, task in list(self.tasks.items()):
            if name in ("recorder_bridge", "recorder"):
                continue
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Task cleanup timeout", task=name)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error("Task cleanup error", task=name, error=str(e))

        self.tasks.clear()
        self._teardown_bootstrap()

        if self.autonomy_monitor is not None:
            try:
                await self.autonomy_monitor.stop()
            except Exception as exc:
                logger.warning("AutonomyMonitor stop failed", error=str(exc))

        if self.session_governor is not None:
            try:
                await self.session_governor.stop()
            except Exception as exc:
                logger.warning("SessionGovernor stop failed", error=str(exc))

        self.evidence_writer.record_transition(
            scope="platform",
            mode="CLOSED",
            reason="system_stop",
            manual_rearm_required=False,
        )
        logger.info("System stopped and tasks cleaned up")

    def stop(self):
        """Synchronous stop (schedules async cleanup if loop is running)."""
        self.running = False
        _watchdog = getattr(self, "_loop_watchdog", None)
        if _watchdog is not None:
            _watchdog.stop()
        self.md_service.running = False
        self.exec_service.running = False
        self.risk_engine.running = False
        self.recon_service.running = False
        self.strategy_runner.running = False
        self.execution_gateway.stop()
        self.session_hook_manager.stop()
        self.health_server.stop()

        if getattr(self, "gateway_service", None) is not None:
            self.gateway_service.running = False

        loop = getattr(self, "loop", None)
        if loop is not None and loop.is_running():
            self._stop_async_task = asyncio.create_task(self.stop_async())
        else:
            self._sync_drain_recorder()
            for cn in ("md_client", "order_client"):
                self._close_broker_client(cn)
            self._teardown_bootstrap()
            self.evidence_writer.record_transition(
                scope="platform",
                mode="CLOSED",
                reason="system_stop",
                manual_rearm_required=False,
            )

    async def _cleanup_tasks(self):
        """Cancel and await all running tasks."""
        for name, task in list(self.tasks.items()):
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception as e:
                    logger.error("Task cleanup error", task=name, error=str(e))
        self.tasks.clear()
        self._teardown_bootstrap()

    def _persist_lost_exec_event(self, event) -> None:
        """Best-effort persist of exec events that would otherwise be lost.

        Appends serialized event to `.state/exec_overflow_dlq.jsonl` so operators
        can replay them later. Non-blocking (sync write) — acceptable because this
        is the last-resort path after all queues/buffers are exhausted.
        """
        try:
            import orjson as _json_mod

            def _ser(obj):
                return _json_mod.dumps(obj)
        except ImportError:
            import json as _json_mod  # type: ignore[no-redef]

            def _ser(obj):
                return _json_mod.dumps(obj, separators=(",", ":")).encode("utf-8")

        try:
            payload = {
                "topic": getattr(event, "topic", "unknown"),
                "data": getattr(event, "data", {}),
                "ingest_ts_ns": getattr(event, "ingest_ts_ns", 0),
                "lost_at_ns": timebase.now_ns(),
            }
            dlq_path = os.path.join(os.getenv("HFT_STATE_DIR", ".state"), "exec_overflow_dlq.jsonl")
            os.makedirs(os.path.dirname(dlq_path), exist_ok=True)
            with open(dlq_path, "ab") as f:
                f.write(_ser(payload) + b"\n")
        except Exception as exc:
            logger.error("exec_overflow_dlq_write_failed", error=str(exc))

    def _safe_enqueue_exec(self, event) -> None:
        """Enqueue exec event with overflow buffer fallback."""
        from hft_platform.observability.metrics import MetricsRegistry

        try:
            self.raw_exec_queue.put_nowait(event)
        except asyncio.QueueFull:
            buf_len = len(self._exec_overflow_buf)
            if buf_len >= self._EXEC_OVERFLOW_MAX:
                self._exec_overflow_evicted += 1
                MetricsRegistry.get().exec_overflow_evicted_total.inc()
                logger.critical(
                    "exec_overflow_buf FULL — fill persisted to DLQ file",
                    evicted_count=self._exec_overflow_evicted,
                    event_topic=getattr(event, "topic", "?"),
                )
                self._persist_lost_exec_event(event)
                self.storm_guard.trigger_halt("exec_overflow_buf_exhausted")
                return
            self._exec_overflow_buf.append(event)
            self._exec_overflow_counter += 1
            MetricsRegistry.get().exec_queue_overflow_total.inc()
            logger.critical(
                "raw_exec_queue FULL — fill routed to overflow buffer",
                overflow_count=self._exec_overflow_counter,
                buf_depth=buf_len + 1,
            )
            if self._exec_overflow_counter >= 3:
                self.storm_guard.trigger_halt("exec_queue_overflow_repeated")

    def _on_exec(self, topic, data):
        loop = getattr(self, "loop", None)
        from hft_platform.execution.normalizer import RawExecEvent

        if topic in ("deal", "order") and hasattr(self, "order_adapter") and self.order_adapter is not None:
            _payload = data.get("payload", data) if isinstance(data, dict) else data
            if isinstance(_payload, dict):
                _get = _payload.get
                _order = _payload.get("order")
                _full_code = _payload.get("full_code")
                _code = _payload.get("code")
                _action = _payload.get("action")
                _id_candidates = [
                    _payload.get("ordno"),
                    _payload.get("ord_no"),
                    _payload.get("seqno"),
                    _payload.get("seq_no"),
                    _payload.get("order_id"),
                    _payload.get("id"),
                    _payload.get("custom_field"),
                ]
                if isinstance(_order, dict):
                    _id_candidates.extend(
                        [
                            _order.get("ordno"),
                            _order.get("ord_no"),
                            _order.get("seqno"),
                            _order.get("seq_no"),
                            _order.get("order_id"),
                            _order.get("id"),
                            _order.get("custom_field"),
                        ]
                    )
            else:
                _full_code = getattr(_payload, "full_code", None)
                _code = getattr(_payload, "code", None)
                _action = getattr(_payload, "action", None)
                _order = getattr(_payload, "order", None)
                _id_candidates = [
                    getattr(_payload, "ordno", None),
                    getattr(_payload, "ord_no", None),
                    getattr(_payload, "seqno", None),
                    getattr(_payload, "seq_no", None),
                    getattr(_payload, "order_id", None),
                    getattr(_payload, "id", None),
                    getattr(_payload, "custom_field", None),
                ]
                if _order is not None:
                    _id_candidates.extend(
                        [
                            getattr(_order, "ordno", None),
                            getattr(_order, "ord_no", None),
                            getattr(_order, "seqno", None),
                            getattr(_order, "seq_no", None),
                            getattr(_order, "order_id", None),
                            getattr(_order, "id", None),
                            getattr(_order, "custom_field", None),
                        ]
                    )
            if _action is None:
                _action = _payload_field(_order, "action")
            if _full_code is None and _code is None:
                _contract = _payload_field(_payload, "contract")
                _full_code = _payload_field(_contract, "full_code")
                _code = _payload_field(_contract, "code")

            _resolved = None
            _strong_ids = [
                str(v)
                for src in (_payload, _order)
                for v in (_payload_field(src, "ordno"), _payload_field(src, "ord_no"))
                if v
            ]
            _ids = _strong_ids + [str(v) for v in _id_candidates if v and str(v) not in _strong_ids]
            resolver = getattr(self.order_adapter, "order_id_resolver", None)
            if resolver is not None and _ids:
                _resolved = resolver.resolve_strategy_id_from_candidates(_ids, strong=len(_strong_ids))
                if _resolved == "UNKNOWN":
                    _resolved = None
            if _resolved is None and _action:
                _symbols = [str(v) for v in (_full_code, _code) if v]
                if _symbols:
                    if topic == "deal":
                        _resolved = self.order_adapter.resolve_strategy_from_deal_candidates(_symbols, str(_action))
                    else:
                        _resolved = self.order_adapter.peek_strategy_from_pending_candidates(_symbols, str(_action))
            if _resolved and isinstance(data, dict):
                data["_resolved_strategy_id"] = _resolved
            if topic == "order" and _action and isinstance(data, dict) and not data.get("_resolved_order_key"):
                _symbols_ok = [str(v) for v in (_full_code, _code) if v]
                if _symbols_ok:
                    _order_key = self.order_adapter.peek_order_key_from_pending_candidates(_symbols_ok, str(_action))
                    if _order_key:
                        data["_resolved_order_key"] = _order_key

        event = RawExecEvent(topic, data, timebase.now_ns())
        if not self.running:
            if len(self._exec_overflow_buf) < self._EXEC_OVERFLOW_MAX:
                self._exec_overflow_buf.append(event)
            else:
                self._exec_overflow_evicted += 1
                logger.critical(
                    "exec_overflow_buf_full_pre_start — fill persisted to DLQ file",
                    evicted_count=self._exec_overflow_evicted,
                    event_topic=topic,
                )
                self._persist_lost_exec_event(event)
                self._exec_startup_overflow_lost = True
            return
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._safe_enqueue_exec, event)
            except RuntimeError:
                if len(self._exec_overflow_buf) < self._EXEC_OVERFLOW_MAX:
                    self._exec_overflow_buf.append(event)
                else:
                    self._exec_overflow_evicted += 1
                    self._persist_lost_exec_event(event)
                    logger.critical(
                        "exec_overflow_buf FULL during loop shutdown — fill persisted to DLQ",
                        evicted_count=self._exec_overflow_evicted,
                    )
        else:
            if len(self._exec_overflow_buf) >= self._EXEC_OVERFLOW_MAX:
                self._exec_overflow_evicted += 1
                try:
                    from hft_platform.observability.metrics import MetricsRegistry

                    MetricsRegistry.get().exec_overflow_evicted_total.inc()
                except Exception:
                    pass
                logger.critical(
                    "exec_overflow_buf FULL in broker thread — fill persisted to DLQ file",
                    evicted_count=self._exec_overflow_evicted,
                    event_topic=getattr(event, "topic", "?"),
                )
                self._persist_lost_exec_event(event)
                self._exec_startup_overflow_lost = True
                return
            self._exec_overflow_buf.append(event)

    async def _recorder_bridge(self):
        """Bridge all Bus events to Recorder."""
        if self._md_record_direct and self._fill_record_direct and self._order_record_direct:
            logger.info(
                "recorder_bridge_early_exit",
                reason="all_direct_recording_enabled",
            )
            return
        batch_size = int(os.getenv("HFT_BUS_BATCH_SIZE", "0") or "0")
        consumer = (
            self.bus.consume_batch(batch_size, start_cursor=-1, consumer_name="recorder_bridge")
            if batch_size > 1
            else self.bus.consume(start_cursor=-1, consumer_name="recorder_bridge")
        )
        from hft_platform.contracts.execution import FillEvent, OrderEvent
        from hft_platform.events import BidAskEvent, TickEvent
        from hft_platform.observability.metrics import MetricsRegistry
        from hft_platform.recorder.mapper import map_event_to_record

        metadata = self.symbol_metadata
        price_codec = PriceCodec(self.price_scale_provider)
        try:
            async for item in consumer:
                batch = item if isinstance(item, list) else [item]
                for event in batch:
                    if self._md_record_direct and isinstance(event, (TickEvent, BidAskEvent)):
                        continue
                    if self._fill_record_direct and isinstance(event, FillEvent):
                        continue
                    if self._order_record_direct and isinstance(event, OrderEvent):
                        continue
                    if isinstance(event, TickEvent) and not self._recorder_seen_tick:
                        self._recorder_seen_tick = True
                        logger.info("Recorder saw Tick event", symbol=event.symbol)
                    elif isinstance(event, BidAskEvent) and not self._recorder_seen_bidask:
                        self._recorder_seen_bidask = True
                        logger.info("Recorder saw BidAsk event", symbol=event.symbol, snapshot=event.is_snapshot)
                    mapped = map_event_to_record(event, metadata, price_codec)
                    if not mapped:
                        continue
                    topic, payload = mapped
                    if self._recorder_drop_on_full:
                        try:
                            self.recorder_queue.put_nowait({"topic": topic, "data": payload})
                        except asyncio.QueueFull:
                            self._recorder_bridge_drops += 1
                            MetricsRegistry.get().recorder_bridge_drops_total.labels(topic=topic).inc()
                            if self._recorder_bridge_drops % 100 == 1:
                                logger.warning(
                                    "recorder_bridge_queue_full",
                                    topic=topic,
                                    drops=self._recorder_bridge_drops,
                                )
                    else:
                        await self.recorder_queue.put({"topic": topic, "data": payload})
        except asyncio.CancelledError:
            pass
