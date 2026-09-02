import asyncio
import collections
import gc
import math
import os
import time
from typing import Any, Dict, Optional

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.core.session_hooks import SessionHookManager
from hft_platform.observability.health import HealthServer
from hft_platform.ops import quarantine_requests, rearm_requests
from hft_platform.ops.evidence import get_shared_autonomy_evidence_writer
from hft_platform.ops.manual_rearm import ManualRearmGate
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.bootstrap import SystemBootstrapper, resolve_order_mode
from hft_platform.services.heartbeat import DEFAULT_HEARTBEAT_PATH, heartbeat_writable, write_heartbeat
from hft_platform.services.loop_watchdog import LoopStallWatchdog
from hft_platform.utils.logging import configure_logging

logger = get_logger("system")


def _read_kill_switch_reason(path: str) -> str:
    """Read kill-switch reason from JSON file. Runs in executor thread."""
    import json as _json

    with open(path, "r") as f:
        data = _json.load(f)
    return data.get("reason", "unknown")


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


#: Distinguishes "caller did not supply state" from "the shared read failed and
#: returned None". Without it the failure path read runtime_state.json twice and
#: logged two warnings on every one-second supervisor tick.
_STATE_NOT_SUPPLIED: Any = object()


class HFTSystem:
    # Latched reference instrument for the drift-burst detector (see
    # ``_drift_burst_book``). Empty until the first book with a valid mid.
    _drift_burst_symbol: str

    # Largest idle-probe overshoot since the supervisor last read it, and how
    # many probe samples have been taken. Class-level defaults so instances
    # built without ``__init__`` (tests use ``__new__``) still read cleanly, and
    # so the supervisor can tell "the loop was clean" (peak 0.0, samples > 0)
    # from "the probe never ran" (samples == 0), which must fall back rather
    # than report a confident zero.
    _loop_probe_peak_ms: float = 0.0
    _loop_probe_samples: int = 0

    # -- Typed helpers to replace hasattr probes ----------------------------------

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

    @staticmethod
    def _get_drawdown_pct(position_store: Any, settings: Dict[str, Any]) -> float:
        """Derive drawdown percentage from position store, or 0.0 if unavailable."""
        dd_fn = getattr(position_store, "get_drawdown_pct", None)
        if dd_fn is not None:
            return float(dd_fn())
        total_pnl = getattr(position_store, "total_pnl", None)
        if total_pnl is not None and total_pnl < 0:
            base_capital = settings.get("base_capital", 10_000_000)
            return total_pnl / base_capital if base_capital > 0 else 0.0
        return 0.0

    @staticmethod
    def _combine_drawdown_with_mtm(
        realized_drawdown_pct: float,
        unrealized_scaled: int,
        base_capital: int,
        price_scale: int = 10_000,
    ) -> float:
        """Combine realized drawdown with unrealized MtM loss.

        Bug 11 (2026-04-17): unrealized is scaled int (x10000, from MtMCalculator),
        base_capital is raw NTD from settings. Dividing directly inflated phantom
        drawdown by 10,000x — a 2 pt move on 1 Mini TAIEX contract produced
        -200bps HALT. Fix: descale unrealized to raw NTD before dividing.

        Args:
            realized_drawdown_pct: positive fraction (0.0-1.0) from PositionStore.
            unrealized_scaled: scaled int (x price_scale); only losses matter.
            base_capital: raw NTD portfolio capital.
            price_scale: scaling factor for unrealized_scaled.

        Returns:
            Adjusted drawdown_pct (positive fraction).
        """
        if base_capital <= 0 or price_scale <= 0:
            return realized_drawdown_pct
        # Bug 11/17: guard against NaN/inf unrealized (e.g. from unexpected float
        # arithmetic upstream). Division of NaN/inf inflates drawdown_pct to NaN/inf,
        # and the downstream ``-int(drawdown_pct * 10_000)`` raises ValueError which
        # the broad ``supervise()`` exception handler silently swallows, skipping
        # the StormGuard update for that cycle.
        try:
            if math.isnan(unrealized_scaled) or math.isinf(unrealized_scaled):
                logger.warning(
                    "combine_drawdown_nonfinite_unrealized",
                    unrealized_scaled=unrealized_scaled,
                )
                return realized_drawdown_pct
        except TypeError:
            # unrealized_scaled is not a number (shouldn't happen); fall through.
            return realized_drawdown_pct
        if unrealized_scaled >= 0:
            return realized_drawdown_pct
        unrealized_ntd = unrealized_scaled / price_scale
        result = realized_drawdown_pct - unrealized_ntd / base_capital
        if math.isnan(result) or math.isinf(result):
            logger.warning(
                "combine_drawdown_nonfinite_result",
                realized_drawdown_pct=realized_drawdown_pct,
                unrealized_scaled=unrealized_scaled,
                base_capital=base_capital,
                price_scale=price_scale,
            )
            return realized_drawdown_pct
        return result

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

        # Pick the most liquid book rather than the first one the dict happens
        # to yield. Latching fixed the *rotation* but not the *choice*: in
        # production it latched EXFH6 — a far-month contract quoting a
        # 48-point spread — and drove ~346 platform-wide toxicity bursts a day
        # off the thinnest book on the system. Relative spread is the cheapest
        # liquidity proxy the book already carries, so this needs no extra
        # state, config, or symbol list that would go stale on a roll.
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
            # A non-positive spread is a locked or half-built book, not a
            # liquidity win — it must not outrank every real two-sided quote.
            if spread <= 0:
                continue
            # spread/mid compared by cross-multiplication: integer arithmetic
            # only, per core law 4 (no float price math). The x2 factor in
            # mid_price_x2 appears on both sides and cancels.
            if best_book is None or spread * best_mid_x2 < best_spread * mid_x2:
                best_symbol, best_book, best_spread, best_mid_x2 = symbol, book, spread, mid_x2

        if best_book is not None:
            chosen_symbol, chosen_book, chosen_spread = best_symbol, best_book, best_spread
        elif fallback_book is not None:
            # Every book is locked/one-sided. Feeding the detector nothing would
            # silently disable the toxicity gate, so keep the old behaviour.
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
        # H9: pre-initialize loop so early broker-thread callbacks (which can
        # fire between __init__ and run()'s first tick) can do an is-None
        # check without tripping AttributeError. Assigned for real at run().
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # In-process event-loop stall watchdog (force-exit on starvation so the
        # container restart policy recovers a spinning/hung loop). Constructed
        # and started in run(); referenced by stop_async() for clean teardown.
        self._loop_watchdog: LoopStallWatchdog | None = None
        self._recorder_seen_tick = False
        self._recorder_seen_bidask = False
        self._md_record_direct = os.getenv("HFT_MD_RECORD_DIRECT", "1").lower() not in {"0", "false", "no", "off"}
        self._fill_record_direct = True  # Always use direct fill recording when recorder_queue is wired
        self._order_record_direct = True  # Always use direct order recording (H5: prevent ring buffer overwrite)

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
        # Request ids already reported as belonging to another engine's
        # quarantine. The supervisor rescans the directory every tick and a
        # foreign request is never consumed, so without this the same file
        # would produce a warning per tick forever.
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
        # D1: Wire overflow buffer to router (buffer lives on system, router drains it)
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
        # Request ids already applied to the governor, so a duplicate read of
        # the same request within one tick cannot re-arm twice.

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
            self.order_adapter.set_storm_guard(self.storm_guard)  # M1: live HALT check
        else:
            self.order_adapter._storm_guard = self.storm_guard

        # Post-reconnect: invalidate stale live orders (they are dead at broker side)
        if hasattr(self.md_service, "register_on_reconnect"):
            self.md_service.register_on_reconnect(
                lambda reason: self.order_adapter.invalidate_live_orders(reason=reason)
            )
            # Reset stale event counter after reconnect so logs are per-session
            self.md_service.register_on_reconnect(lambda reason: self.strategy_runner.reset_stale_counter())
        self.recon_service.platform_degrade_controller = self.platform_degrade_controller

        self._halt_log_mono: float = 0.0  # rate-limit HALT log to avoid spam
        self._halt_checkpoint_written: bool = False  # write checkpoint once on HALT entry
        self._drift_burst_symbol = ""
        self._mtm_calculator = None
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
        self._task_started_at: Dict[str, float] = {}  # name → monotonic start time
        self._task_restart_base_delay_s = self._env_float("HFT_TASK_RESTART_BACKOFF_S", 1.0, min_value=0.1)
        self._task_restart_max_delay_s = self._env_float("HFT_TASK_RESTART_BACKOFF_MAX_S", 30.0, min_value=0.1)
        self._task_restart_max_attempts = int(os.getenv("HFT_TASK_RESTART_MAX_ATTEMPTS", "10"))
        self._task_healthy_uptime_s = self._env_float("HFT_TASK_HEALTHY_UPTIME_S", 60.0, min_value=5.0)
        self._queue_log_every_s = self._env_float("HFT_SUPERVISOR_QUEUE_LOG_EVERY_S", 30.0, min_value=1.0)
        self._last_queue_log_s = 0.0
        self._recorder_bridge_drops: int = 0
        self._pnl_snapshot_drops: int = 0

        # WU-11: Session hooks (disabled by default)
        self.session_hook_manager = SessionHookManager()

        # WU-17: Structured health endpoint
        self.health_server = HealthServer(system=self)

        # P1 fix: handle for stop_async() task created by synchronous stop().
        # The launcher (main.py) awaits this to prevent loop teardown from
        # cutting recorder drain / final checkpoint / order drain short.
        self._stop_async_task: asyncio.Task[None] | None = None

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()

        # Bind the engine loop into StormGuard so halt-callback coroutines scheduled
        # from non-asyncio threads (e.g. bootstrap lease-refresh daemon) can be
        # dispatched via run_coroutine_threadsafe instead of get_running_loop()
        # (which raises RuntimeError in a non-loop thread). P0-I4.
        sg = getattr(self, "storm_guard", None)
        if sg is not None and hasattr(sg, "bind_loop"):
            sg.bind_loop(self.loop)

        # Schedule coroutines that build() collected while outside the engine loop.
        # P0-I1: build() runs in __init__ without a running loop, so it cannot call
        # asyncio.get_event_loop() safely on Python 3.12+. Coroutines returned from
        # build() (e.g. config-snapshot writer, alertmanager bridge) are started here.
        self._deferred_tasks: list[asyncio.Task[Any]] = []
        registry = getattr(self, "registry", None)
        for coro in list(getattr(registry, "deferred_tasks", None) or []):
            try:
                self._deferred_tasks.append(self.loop.create_task(coro))
            except Exception as exc:  # noqa: BLE001
                logger.warning("deferred_task_schedule_failed", error=str(exc))

        # Check for fills lost during startup race (before loop was assigned)
        if self._exec_startup_overflow_lost:
            logger.critical(
                "exec_startup_overflow_halt",
                msg="Fills were LOST during startup race — triggering HALT",
                evicted_count=self._exec_overflow_evicted,
            )
            self.storm_guard.trigger_halt("exec_overflow_startup_race")
        _gc_disabled = False

        import signal

        try:
            self.loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        except (NotImplementedError, OSError):
            pass

        logger.info("System Starting...")
        # Lifecycle boundary, not the trading loop: nothing is trading yet, and
        # `manual_rearm_required=False` on a non-ack transition never reaches the
        # locking store anyway -- only the audit trail. Left synchronous.
        self.evidence_writer.record_transition(
            scope="platform",
            mode="NORMAL",
            reason="system_start",
            manual_rearm_required=False,
        )

        try:
            # Keep liveness observable while broker login or recovery blocks.
            self._start_service("health_server", self.health_server.run())

            order_mode = getattr(self, "order_mode", None)
            if order_mode is None:
                order_mode = resolve_order_mode()
                self.order_mode = order_mode
            orders_enabled = order_mode != "disabled"

            # Login order_client (separate Shioaji session for execution).
            # md_client logs in via MarketDataService._connect_sequence(), but
            # order_client needs its own login for contract resolution + callbacks.
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
                    self.order_client.set_execution_callbacks(
                        on_order=lambda state, payload: self._on_exec("order", {"state": state, "payload": payload}),
                        on_deal=lambda payload: self._on_exec("deal", {"payload": payload}),
                    )
                else:
                    logger.error("order_client login returned false — orders are unavailable", order_mode=order_mode)
                    if order_mode == "live":
                        logger.critical("live_order_login_failed_startup_blocked")
                        return
            else:
                logger.warning("order_path_disabled_quote_only")

            # Order governance is unnecessary in quote-only mode and can invoke
            # flatten/recovery paths, so keep it outside the quote runtime.
            if orders_enabled and self.session_governor is not None:
                await self.session_governor.start()
                logger.info("SessionGovernor started")

            # Opt-in: start AutonomyMonitor via supervisor (so crashes are detected/restarted)
            if orders_enabled and self.autonomy_monitor is not None:
                self._start_service("autonomy_monitor", self.autonomy_monitor.run())

            # Bug 27 (2026-04-17): start PositionStuckMonitor (alert-only observability).
            if orders_enabled and self.position_stuck_monitor is not None:
                self._start_service("position_stuck_monitor", self.position_stuck_monitor.run())

            # Start Services
            # Recorder MUST start before exec_router to prevent fill recording gaps
            # during startup (fills can arrive as soon as execution callbacks are wired).
            self._start_service("recorder", self.recorder.run())
            # Save bus cursor BEFORE MarketDataService starts publishing events.
            # StrategyRunner will replay from this cursor to avoid missing startup-window events.
            pre_md_cursor = self.bus.cursor
            self._start_service("md", self.md_service.run())
            # C3: Position recovery MUST complete before exec_router so that
            # exec_router does not apply startup-window fills on top of a
            # stale position_store only to be overwritten by the broker
            # snapshot. Fills arriving during recover() pile up in
            # raw_exec_queue (bounded, with overflow buffer) and are consumed
            # only after the canonical baseline is loaded.
            if orders_enabled and os.getenv("HFT_STARTUP_RECON_ENABLED", "1") == "1" and self.startup_verifier:
                try:
                    recovery = await self.startup_verifier.recover(
                        account_id=self.registry.account_id or self.registry.broker_id,
                    )
                    if recovery.halted:
                        logger.critical(
                            "Position recovery HALT — refusing to start trading",
                            source=recovery.source,
                            mismatches=recovery.mismatches,
                        )
                        return
                    logger.info(
                        "Position recovery complete",
                        source=recovery.source,
                        loaded=recovery.positions_loaded,
                        corrected=recovery.auto_corrected,
                    )
                except Exception as exc:
                    logger.critical("Position recovery failed", error=str(exc))
                    return

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
                # CE-M2: start GatewayService when enabled; otherwise start RiskEngine standalone
                if self.gateway_service is not None:
                    self._start_service("gateway", self.gateway_service.run())
                else:
                    self._start_service("risk", self.risk_engine.run())
                self._start_service("order", self.order_adapter.run())
                self._start_service("exec_gateway", self.execution_gateway.run())

                # ── Checkpoint Writer (after recovery, before trading) ──
                if os.getenv("HFT_CHECKPOINT_ENABLED", "1") == "1" and self.checkpoint_writer:
                    self._start_service("checkpoint_writer", self.checkpoint_writer.run())

                self._start_service("recon", self.recon_service.run())
                # Pass saved pre-MD cursor so StrategyRunner replays events published during startup
                self.strategy_runner.set_start_cursor(pre_md_cursor)
                self._start_service("strat", self.strategy_runner.run())
                if (
                    hasattr(self.strategy_runner, "_rejection_queue")
                    and self.strategy_runner._rejection_queue is not None
                ):
                    self._start_service("rejection_consumer", self.strategy_runner._run_rejection_consumer())
            else:
                logger.info("quote_only_service_plane_active", services=["health_server", "recorder", "md"])

            # Start AuditWriter flush tasks (singleton, lazy-created by RiskEngine/StormGuard)
            try:
                from hft_platform.recorder.audit import get_audit_writer

                self._audit_writer = get_audit_writer()
                # P1-a (2026-04-27): wire the recorder's DataWriter so audit
                # rows actually land in ClickHouse. Previously this call was
                # `get_audit_writer()` with no writer arg; AuditWriter._writer
                # stayed None and every batch fell through to the structlog
                # ``audit_fallback`` log path (Bug #19), leaving audit.*
                # tables empty.
                #
                # Companion fix: 20260427_001_audit_schema_alignment.sql
                # rewrites the DDL to match producer payloads — without that
                # migration this wiring would just trade silent fallback for
                # noisy CH write errors.
                _recorder_writer = _audit_persistence_writer_for_recorder(self.recorder)
                if _recorder_writer is not None and hasattr(self._audit_writer, "set_writer"):
                    self._audit_writer.set_writer(_recorder_writer)
                await self._audit_writer.start()
                if getattr(self._audit_writer, "_writer", None) is None:
                    # Recorder writer was unavailable at start time (rare —
                    # implies recorder failed to construct). Keep the warning
                    # so operators still see the degraded mode.
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
                # Inject audit writer into OrderAdapter for order lifecycle logging
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

            # WU-11: Session hooks
            if orders_enabled and self.session_hook_manager.enabled:
                self._start_service("session_hooks", self.session_hook_manager.run())

            # Disable GC during active trading (HFT Core Law 1: Allocator Law)
            if os.getenv("HFT_GC_DISABLE_TRADING", "0").strip().lower() in {"1", "true", "yes", "on"}:
                gc.disable()
                _gc_disabled = True
                logger.info("GC disabled for trading session")

            # Start the event-loop stall watchdog before entering the supervisor
            # loop. It runs on a dedicated OS thread and force-exits the process
            # if _supervise() stops beating (loop spin/starvation) past the
            # threshold, so the container restart policy recovers the engine in
            # seconds. Disabled via HFT_LOOP_STALL_KILL_S<=0.
            self._loop_watchdog = LoopStallWatchdog(
                stall_kill_s=self._env_float("HFT_LOOP_STALL_KILL_S", 60.0, 0.0),
                check_interval_s=self._env_float("HFT_LOOP_STALL_CHECK_S", 5.0, 0.1),
            )
            self._loop_watchdog.start()

            # Independent loop-congestion probe. Not in _iter_supervised_services:
            # it is a measurement, and a measurement dying must never HALT the
            # platform. It is cancelled with the rest of self.tasks on shutdown.
            self._start_service("loop_probe", self._probe_event_loop_lag())

            # Start Monitor/Supervisor Loop
            await self._supervise()

        except asyncio.CancelledError:
            logger.info("System Stopping...")
        finally:
            if _gc_disabled:
                gc.enable()
                logger.info("GC re-enabled after trading session")
            # Use stop_async() for ordered shutdown: bridge → recorder drain → tasks.
            # The sync stop() skips the recorder drain path, risking data loss.
            await self.stop_async()

    def _start_service(self, name, coro):
        if name in {"exec_router", "exec_gateway"}:
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                metrics = MetricsRegistry.get()
                if name == "exec_router":
                    metrics.execution_router_alive.set(1)
                elif name == "exec_gateway":
                    metrics.execution_gateway_alive.set(1)
            except Exception as _exc:  # noqa: BLE001
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
        # H10: single-path shutdown — if recorder.run()'s finally has
        # already drained+flushed, skip the sync fallback. Double-drive
        # would race on writer/batchers/WALFirstWriter state. Strict
        # ``is True`` so test doubles (MagicMock auto-creates truthy
        # attributes) do not accidentally short-circuit.
        if getattr(recorder, "_shutdown_drained", False) is True:
            logger.info("sync_drain_recorder_skipped_already_drained")
            return

        # P0-I5: stop the WAL batch timer thread BEFORE the temp loop runs so
        # that the temp loop becomes the sole writer to the WAL directory.
        # Walk both possible WAL writer holders (DataWriter-embedded batch
        # writer AND WAL-first writer's embedded batch writer).
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
            except Exception as exc:  # noqa: BLE001
                logger.warning("wal_batch_writer_stop_failed", error=str(exc))

        wal_first = getattr(recorder, "_wal_first_writer", None)
        if wal_first is not None:
            inner = getattr(wal_first, "_wal", None)
            if inner is not None and hasattr(inner, "stop"):
                try:
                    inner.stop()
                except Exception as exc:  # noqa: BLE001
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
                positions_snap = dict(self.position_store.positions)
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

        # 1. Clear checkpoint file
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

        # 2. Clear pending recovery positions in PositionStore
        recovery = getattr(self.position_store, "_recovery_positions", None)
        if recovery is not None:
            count = len(recovery)
            recovery.clear()
            results["recovery_positions"] = f"cleared {count} entries"
            logger.info("graceful_reset: recovery positions cleared", count=count)
        else:
            results["recovery_positions"] = "no recovery state"

        # 3. Clear DLQ state
        try:
            from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

            dlq = get_orphaned_fill_dlq()
            dlq_count = dlq.count
            dlq.drain()
            results["fill_dlq"] = f"drained {dlq_count} entries"
            logger.info("graceful_reset: fill DLQ drained", count=dlq_count)
        except Exception as exc:
            results["fill_dlq"] = f"error: {exc}"

        # 4. Reset StormGuard to NORMAL (if in HALT from reconciliation)
        if self.storm_guard.state >= StormGuardState.STORM:
            old_state = self.storm_guard.state.name
            self.storm_guard._halt_entry_ts = 0.0
            self.storm_guard._storm_entry_ts = 0.0
            self.storm_guard._de_escalate_count = self.storm_guard._de_escalate_threshold
            # Force de-escalation on next update cycle
            self.storm_guard.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
            results["storm_guard"] = f"reset from {old_state} to {self.storm_guard.state.name}"
            logger.info("graceful_reset: storm_guard reset", old=old_state, new=self.storm_guard.state.name)
        else:
            results["storm_guard"] = "already NORMAL"

        # 5. Exit REDUCE_ONLY if active
        if self.platform_degrade_controller.reduce_only_active:
            await self.platform_degrade_controller.exit_reduce_only_async(reason=reason)
            results["reduce_only"] = "exited"
            logger.info("graceful_reset: REDUCE_ONLY exited", reason=reason)
        else:
            results["reduce_only"] = "not active"

        # 6. Reset reconciliation state
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

        # INFRA-018: Prevent infinite crash-loop restart oscillation.
        if attempt > self._task_restart_max_attempts:
            logger.critical(
                "Service exceeded max restart attempts — permanently stopped",
                task=name,
                component=component,
                attempts=attempt - 1,
                max_attempts=self._task_restart_max_attempts,
            )
            # Trigger permanent HALT so trading stops cleanly.
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
        # Read the live reasons BEFORE the re-arm is consumed. The re-arm has to
        # re-apply them in memory before it suspends, or clearing here and
        # re-checking below leaves allow_open() true across the audit write --
        # a fail-open during exactly the condition reduce-only contains.
        reasons = inputs.reduce_only_reasons() if inputs is not None else []
        await self._consume_platform_rearm_request_async(state, reasons)
        await self._consume_strategy_rearm_requests(state)
        # After the re-arm, deliberately: if an operator has both a re-arm
        # and a quarantine pending for the same strategy, the tick must end
        # quarantined. Stopping is the fail-closed direction.
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
        except Exception as exc:  # noqa: BLE001 - a failed delete must not stop supervision
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
        except Exception as exc:  # noqa: BLE001 - a failed delete must not stop supervision
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
        except Exception as exc:  # noqa: BLE001 - a scan failure must not stop supervision
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
                # Already latched. Retire the request rather than re-latching:
                # a second quarantine would mint a new token and orphan any
                # re-arm request the operator has already written against the
                # current one.
                if not await asyncio.to_thread(self._retire_quarantine_request, request):
                    continue
                logger.warning(
                    "strategy_quarantine_request_already_quarantined",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                )
                continue
            # Apply BEFORE consuming, the opposite of the re-arm path and for
            # the same reason: fail closed. If the process dies between the two
            # the request is reapplied next boot, and re-quarantining is a
            # no-op. Consuming first would risk losing the stop entirely.
            try:
                await governor.quarantine_async(request.strategy_id, reason=request.reason)
            except Exception as exc:  # noqa: BLE001 - one bad request must not stop the rest
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
            # Converge before consuming: a request in this same tick must be
            # able to clear a latch adopted a few lines above it.
            try:
                governor.reconcile_persisted_quarantines(state)
            except Exception as exc:
                # A document this tick cannot parse must not stop supervision --
                # the boot path is where an unreadable safety document fails
                # closed. Here it means one tick adopts nothing.
                logger.warning("strategy_quarantine_reconcile_failed", error=str(exc))
        try:
            requests = await asyncio.to_thread(rearm_requests.pending, gate.state_path.parent)
        except Exception as exc:
            logger.warning("strategy_rearm_request_scan_failed", error=str(exc))
            return
        for request in requests:
            if not governor.owns_token(request.quarantine_token):
                # Another engine's quarantine. The directory is shared state and
                # the session-ownership preflight is advisory, so two engines can
                # be scanning it; retiring this would unlink an authorization the
                # other engine has not seen yet and the operator would never learn
                # it was destroyed. Not ours to judge, not ours to delete.
                # Lazily, because this method is also exercised against an
                # ``HFTSystem.__new__`` rig that never runs ``__init__``.
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
                # No live quarantine, and after this branch that no longer
                # includes "the engine restarted". It used to: the comment here
                # said a strategy quarantine does not survive a restart, which
                # is the exact defect ``restore_persisted_quarantines`` removes,
                # and leaving it would tell the next reader that retiring a
                # request across a restart is normal. It is not. A restored
                # latch keeps its persisted token, so a request written before
                # the restart still matches and is still applied.
                #
                # What reaches here now: an operator or an earlier request in
                # this same tick cleared the latch, the request names a strategy
                # that was never quarantined, or the pre-restart durable write
                # failed so there was no latch on disk to restore. Retire it so
                # it cannot linger and cannot apply to some future quarantine.
                if not await asyncio.to_thread(self._retire_rearm_request, request):
                    continue
                logger.warning(
                    "strategy_rearm_request_retired_no_live_quarantine",
                    strategy_id=request.strategy_id,
                    request_id=request.request_id,
                )
                continue
            if live_token != request.quarantine_token:
                # Authorizes a different quarantine instance than the live one.
                # Retire it: the operator must look at the current failure.
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
            # Consume before applying. If the process dies between the two, the
            # strategy stays quarantined and the operator reissues -- the
            # fail-closed direction. Consuming after could replay the request.
            # A consume that *fails* must not be followed by the re-arm either,
            # for the same reason: the request would still be on disk to replay.
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

        # Fail LOUD if the file-heartbeat path is not writable. On THESHOW
        # (2026-06-15) this write failed silently for 18h (bind-mounted dir owned
        # by root, container ran as uid 1000), disabling the external watchdog.
        # The in-process LoopStallWatchdog is the primary net now, but surface
        # the misconfiguration so the external watchdog can be relied on too.
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

        # Periodic gen-0 GC: collect short-lived cyclic refs even when full GC is disabled.
        # Gen-0 is typically <1ms and safe to run at supervisor frequency.
        _gc_gen0_interval = max(1, int(os.getenv("HFT_GC_GEN0_INTERVAL_TICKS", "10")))
        _gc_gen0_tick = 0
        _gc_gen0_enabled = os.getenv("HFT_GC_DISABLE_TRADING", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        } and os.getenv("HFT_GC_GEN0_PERIODIC", "1").strip().lower() not in {"0", "false", "no", "off"}
        # Periodic gen-2 GC: reclaim long-lived cyclic refs (structlog, asyncio internals).
        #
        # The default used to be 300 ticks (~5 min), justified by "typically
        # <10 ms". Production disproved that. Measured on THESHOW across 107
        # consecutive runs via the ``gc_gen2_periodic`` log below: the pause is
        # ~40 ms with the feed idle and **100-116 ms during a live session** —
        # it scales with the live object graph, not with uptime — while 105 of
        # those 107 runs reclaimed *zero* objects (111 in total). At 5 min that
        # was ~12 hard event-loop stalls per hour, against 18.5 measured probe
        # excursions above 50 ms per hour — so this is the largest identified
        # contributor to the tail, roughly two thirds of it, not all of it.
        #
        # ``gc.collect()`` holds the GIL, so this cannot be moved to an executor;
        # frequency is the only lever that does not change what gets reclaimed.
        # Hourly still yields 24 collections a day, which is ample for the
        # gen-1/gen-2 accumulation this exists to bound under ``gc.disable()``.
        _gc_gen2_interval = max(60, int(os.getenv("HFT_GC_GEN2_INTERVAL_TICKS", "3600")))
        _gc_gen2_tick = 0
        _gc_gen2_enabled = _gc_gen0_enabled  # same gate as gen-0

        while self.running:
            await asyncio.sleep(interval_s)  # 1Hz Tick
            now_tick = loop.time()
            # This spans the WHOLE tick period (sleep + the body below), so it is
            # loop congestion PLUS this loop's own work — not "event loop lag"
            # despite the metric name. Kept unrenamed because dashboards and
            # alerts query it; ``event_loop_probe_lag_ms`` (an idle probe task)
            # and ``supervisor_tick_duration_ms`` (recorded at the bottom of this
            # body) split it into its two halves and should sum back to it.
            lag_s = max(0.0, now_tick - last_tick - interval_s)
            metrics.event_loop_lag_ms.set(lag_s * 1000.0)
            last_tick = now_tick

            # Liveness beat for the stall watchdog. Recorded first thing each
            # tick: if the loop later spins/blocks, beats stop and the watchdog
            # thread force-exits the process so the container restarts it.
            # ``getattr`` guard mirrors the shutdown paths below: partially
            # constructed instances (tests build via ``__new__``) never set it.
            _watchdog = getattr(self, "_loop_watchdog", None)
            if _watchdog is not None:
                _watchdog.beat()

            # A. Update StormGuard with real metrics
            # INFRA-007: Each computation is isolated so StormGuard.update()
            # always runs even if individual inputs fail.

            # 1. Get feed gap from market data service
            feed_gap_s = 0.0
            try:
                feed_gap_s = self._get_max_feed_gap_s(self.md_service)
            except Exception as e:
                logger.warning("StormGuard feed_gap computation failed", error=str(e))

            # 2. Get drawdown from position store (realized + unrealized)
            drawdown_pct = 0.0
            try:
                drawdown_pct = self._get_drawdown_pct(self.position_store, self.settings)
                if self._mtm_calculator is not None:
                    try:
                        # snapshot() rather than total_unrealized_pnl(): the
                        # bare total reports 0 both for a flat book and for a
                        # book where nothing could be priced, and the daily
                        # stop is allowed to lift only on the former.
                        mtm = self._mtm_calculator.snapshot()
                        unrealized = mtm.total_scaled
                        drawdown_pct = self._combine_drawdown_with_mtm(
                            realized_drawdown_pct=drawdown_pct,
                            unrealized_scaled=int(unrealized),
                            base_capital=int(self.settings.get("base_capital", 10_000_000)),
                        )
                        if not mtm.complete:
                            logger.warning(
                                "mark-to-market incomplete; daily stop cannot release",
                                priced=mtm.priced,
                                unpriced=mtm.unpriced,
                            )
                        self.risk_engine.update_unrealized_pnl(int(unrealized), complete=mtm.complete)
                    except Exception as e:
                        # Was a bare ``pass``. A mark-to-market that fails every
                        # tick also starves the daily-loss reset, and silence
                        # made that indistinguishable from a healthy engine.
                        logger.warning("StormGuard mark-to-market update failed", error=str(e))
            except Exception as e:
                logger.warning("StormGuard drawdown computation failed", error=str(e))

            # The daily-loss reset boundary ticks regardless of the block above:
            # it is what releases a latched daily-loss HALT, and both the
            # drawdown read and the MtM read can fail for reasons that have
            # nothing to do with the calendar.
            try:
                self.risk_engine.roll_daily_loss_boundary()
            except Exception as e:
                logger.warning("daily loss boundary roll failed", error=str(e))

            # 3. Latency input for StormGuard.
            latency_us = self._stormguard_latency_us(lag_s)

            # 3b. Inform StormGuard of session state
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

            # 4. ALWAYS call StormGuard.update() with whatever data we have.
            try:
                drawdown_bps = -int(drawdown_pct * 10_000)
                self.storm_guard.update(
                    drawdown_bps=drawdown_bps,
                    latency_us=latency_us,
                    feed_gap_s=feed_gap_s,
                )
            except Exception as e:
                logger.warning("StormGuard update call failed", error=str(e))

            # 4b. Update StormGuard with LOB-derived drift-burst toxicity.
            #
            # The detector holds ONE instrument's return window, so it has to be
            # fed one instrument. This used to take whatever came first out of
            # ``lob_engine.books`` — a plain dict that evicts on TTL, so "first"
            # silently rotated between contracts and the drift t-statistic ended
            # up measuring the price gap between them (observed: the same
            # detector logging spreads of 3.75 and 48.45 points). The reference
            # symbol is now latched and only re-picked when it goes away.
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

            # 5. Update per-symbol feed gap metrics
            try:
                feed_gap_metric = getattr(metrics, "feed_gap_by_symbol_seconds", None)
                if feed_gap_metric is not None:
                    for symbol, gap in self._get_feed_gaps_by_symbol(self.md_service).items():
                        capped = metrics.cap_symbol(symbol) if metrics else symbol
                        feed_gap_metric.labels(symbol=capped).set(gap)
            except Exception as e:
                logger.warning("StormGuard per-symbol metrics failed", error=str(e))

            # 6. Update per-connection pool metrics (if QuoteConnectionPool)
            _update_pool_metrics = getattr(self.md_client, "update_metrics", None)
            if _update_pool_metrics is not None:
                try:
                    _update_pool_metrics()
                except Exception as e:
                    # QuoteConnectionPool.update_metrics() already counts and
                    # logs its own failures (hft_quote_pool_metrics_*_total),
                    # so reaching here means the pool object itself is broken.
                    # Still non-fatal, but it must not be invisible: these
                    # gauges are the only per-facade liveness signal there is.
                    logger.warning("pool_metrics_update_failed", error=str(e))

            # Kill-switch file check (async to avoid blocking event loop)
            kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
            loop = asyncio.get_running_loop()
            ks_exists = await loop.run_in_executor(None, os.path.exists, kill_switch_path)
            if ks_exists:
                if self.storm_guard.state != StormGuardState.HALT:
                    try:
                        _ks_reason = await loop.run_in_executor(None, _read_kill_switch_reason, kill_switch_path)
                    except Exception:
                        _ks_reason = "kill_switch_file_present"
                    self.storm_guard.trigger_halt(f"KILL_SWITCH_FILE: {_ks_reason}")
                    logger.critical("Kill switch file detected", path=kill_switch_path, reason=_ks_reason)

            # R11-C4: Telegram /stop emergency halt via Redis key.
            # P2-e (2026-04-27): the file-based kill-switch above (lines
            # 991-1002) ALREADY runs every supervise tick regardless of
            # Redis availability — it is the canonical emergency halt path.
            # The Redis-key check here is a secondary signal for /stop bot
            # commands. If Redis is unreachable the file check still runs
            # next tick, so the previous "Redis unavailable — fall back to
            # file-based kill switch" comment was misleading. Replace with a
            # truthful description.
            _redis_halt = getattr(self, "_redis_client", None)
            if _redis_halt is not None and self.storm_guard.state != StormGuardState.HALT:
                try:
                    _halt_val = await loop.run_in_executor(None, _redis_halt.get, "hft:emergency_halt")
                    if _halt_val and str(_halt_val) not in ("0", "b'0'", "None"):
                        self.storm_guard.trigger_halt("TELEGRAM_EMERGENCY_HALT")
                        logger.critical("Telegram /stop emergency halt activated")
                except Exception:
                    # Redis unavailable — kill-switch FILE check at lines
                    # 991-1002 runs every tick and provides the always-on
                    # halt path; nothing more to do here.
                    pass

            t_gateway = self.tasks.get("exec_gateway")
            # Check Health for all critical services
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
                # I2-C1: HALT-stopped services should restart when HALT de-escalates,
                # not trigger a new HALT. Detect by: no exception + running + non-HALT.
                if exc is None and name in ("order", "exec_gateway") and self.storm_guard.state != StormGuardState.HALT:
                    logger.info("Restarting service after HALT de-escalation", task=name)
                    _svc = self.order_adapter if name == "order" else self.execution_gateway
                    self._set_service_running(_svc, True)
                    if self.running:
                        # count_attempt=False: this task exited cleanly because
                        # HALT stopped it. Bringing it back is normal control
                        # flow, not crash recovery, and must not spend the
                        # crash-loop budget.
                        self._try_restart_service(name, component, coro_factory, count_attempt=False)
                    continue
                # Detect immediate failures: task died within 2s of creation.
                # Apply extra backoff penalty to avoid rapid retry budget burn.
                _started = self._task_started_at.get(name)
                _immediate_fail = _started is not None and (timebase.now_s() - _started) < 2.0
                if _immediate_fail:
                    cur = self._task_restart_attempts.get(name, 0)
                    self._task_restart_attempts[name] = cur + 1  # extra penalty
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

            # Update Metrics — offload blocking psutil calls off the event loop
            await loop.run_in_executor(None, metrics.update_system_metrics)
            # Per-facade health check (QuoteConnectionPool isolation).
            # Outside a session, facade feed gaps are expected and a reconnect
            # can never close them, so the pool suppresses *scheduling* while
            # still running the FSM — degraded/recovered transitions stay
            # visible. Suppression is decided inside the pool from
            # MarketCalendar (QuoteConnectionPool.reconnect_allowed), not from
            # the HFT_RECONNECT_HOURS wall-clock window this used to gate on:
            # that window's night leg ran to 05:05 against an 05:00 close and
            # produced a 5-minute relogin storm, while its day leg stopped at
            # 13:35 and left the last 10 minutes of the day session unchecked.
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

            # Periodic stale symbol eviction for FeatureEngine (rate-limited internally)
            _fe = getattr(getattr(self, "md_service", None), "feature_engine", None)
            if _fe is not None:
                try:
                    _fe.evict_stale_symbols()
                except Exception:  # noqa: BLE001
                    pass

            # Periodic TTL sweep for live_orders (rate-limited internally)
            _oa = getattr(self, "order_adapter", None)
            if _oa is not None:
                try:
                    await _oa.sweep_stale_live_orders()
                except Exception:  # noqa: BLE001
                    pass
                # Bug D (2026-04-20): release strategy pending counters for
                # phantoms past TTL — prevents R47 indefinite freeze when broker
                # never sends fill/cancel callback for a phantom.
                try:
                    await _oa.release_stale_phantom_pendings()
                except Exception:  # noqa: BLE001
                    pass

            now = timebase.now_s()
            t_router = self.tasks.get("exec_router")
            if t_router and not t_router.done():
                metrics.execution_router_heartbeat_ts.set(now)
            if t_gateway and not t_gateway.done():
                metrics.execution_gateway_heartbeat_ts.set(now)

            # File-based heartbeat for watchdog monitoring (every ~30s)
            _heartbeat_tick += 1
            if _heartbeat_tick >= _heartbeat_interval_ticks:
                _heartbeat_tick = 0
                write_heartbeat(_heartbeat_path)

            # Check StormGuard State - CRITICAL: Block orders when HALT
            if self.storm_guard.state == StormGuardState.HALT:
                _now_mono = time.monotonic()
                _halt_log_interval_s = 60.0
                if _now_mono - self._halt_log_mono >= _halt_log_interval_s:
                    self._halt_log_mono = _now_mono
                    logger.error("System HALTED by StormGuard - blocking orders")
                # Defense-in-depth: propagate HALT to gateway policy FIRST so the
                # gateway rejects new intents while we drain queues below.
                if self.gateway_service is not None:
                    self.gateway_service.set_halt()
                # M5: Write position checkpoint once on HALT entry, not every tick.
                if self.checkpoint_writer is not None and not self._halt_checkpoint_written:
                    self._halt_checkpoint_written = True
                    try:
                        self.checkpoint_writer.write_checkpoint()
                    except Exception:
                        logger.exception("halt_checkpoint_write_failed")
                # Drain risk queue — preserve safety orders + halt-exempt intents
                risk_drained = 0
                _requeue: list = []
                while not self.risk_queue.empty():
                    try:
                        item = self.risk_queue.get_nowait()
                        self.risk_queue.task_done()
                        _itype = getattr(item, "intent_type", None)
                        _sid = getattr(item, "strategy_id", None)
                        # Preserve: CANCEL/FORCE_FLAT (always safe) + halt-exempt strategies
                        _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                        _is_exempt = bool(_sid) and self.storm_guard.is_halt_exempt(_sid)
                        if _is_safety or _is_exempt:
                            _requeue.append(item)
                        else:
                            risk_drained += 1
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
                # Drain intent_channel (gateway mode) — same safety filter
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
                    # Re-inject safety envelopes via the internal queue (envelope already wrapped)
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
                # Drain order queue — preserve safety commands + halt-exempt
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
                    except asyncio.QueueEmpty:
                        break
                # Safety cmds dispatched directly — execute() handles running=False
                # via _dispatch_to_api(), bypassing the stopped _api_worker queue.
                for cmd in _cmd_requeue:
                    try:
                        _task = asyncio.create_task(self.order_adapter.execute(cmd))
                        _task.add_done_callback(_log_safety_dispatch_error)
                        # Anchor task to prevent GC before completion
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
                # Signal order adapter to stop processing
                self._set_service_running(self.order_adapter, False)
                # H6: Cancel in-flight orders already dispatched to broker
                try:
                    _drain_task = asyncio.create_task(self.order_adapter.drain_and_cancel())
                    _bg = getattr(self.order_adapter, "_background_tasks", None)
                    if _bg is not None:
                        _bg.add(_drain_task)
                        _drain_task.add_done_callback(_bg.discard)
                except Exception as exc:
                    logger.warning("In-flight order cancellation failed during HALT", error=str(exc))
            else:
                # Fix H5: Recover GatewayPolicy from sticky HALT when StormGuard
                # de-escalates. set_normal() is idempotent, safe to call repeatedly.
                if self.gateway_service is not None:
                    self.gateway_service.set_normal()
                # Fix P2-4: Re-enable OrderAdapter after HALT recovery.
                # During HALT we set order_adapter.running=False (line 712);
                # without this, the adapter stays stopped after de-escalation.
                self._set_service_running(self.order_adapter, True)
                # Reset HALT log/checkpoint rate-limiting for next HALT episode.
                self._halt_checkpoint_written = False

            # Periodic gen-0 GC: reclaim cyclic refs from framework objects
            # (structlog, Prometheus, asyncio internals) without full GC pause.
            if _gc_gen0_enabled:
                _gc_gen0_tick += 1
                if _gc_gen0_tick >= _gc_gen0_interval:
                    _gc_gen0_tick = 0
                    gc.collect(0)
            # Periodic gen-2 GC: reclaim long-lived cyclic refs that gen-0 cannot reach.
            # Without this, 24/7 operation with gc.disable() leaks gen-1/gen-2 objects.
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

            # Last statement of the tick body: how long this loop's own work
            # took. Everything above ran between ``now_tick`` and here, so the
            # remainder of ``event_loop_lag_ms`` is congestion this loop did not
            # cause. Attributing the 1 ms budget needs both numbers.
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
        # Drained, not accumulated. The probe is started by _start_service and
        # deliberately left out of _iter_supervised_services, so it can die
        # without HALTing anything -- and a dead probe's peak is 0.0, which a
        # latency breaker reads as a perfectly healthy loop. A cumulative count
        # would keep selecting this branch forever after the first sample and
        # hand StormGuard that confident zero. Draining means a window with no
        # new samples falls back to the supervisor's own signal: noisier, but
        # alive.
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
            # Peak since the supervisor last drained it: this is StormGuard's
            # loop-lag input. Kept here rather than read back off the histogram
            # because a Prometheus histogram cannot report a max.
            if overshoot_ms > self._loop_probe_peak_ms:
                self._loop_probe_peak_ms = overshoot_ms
            self._loop_probe_samples += 1

    async def stop_async(self):
        """Async stop with proper task cleanup."""
        self.running = False
        # Stop the stall watchdog first so an intentional, possibly slow
        # shutdown drain below is never mistaken for a loop stall and killed.
        _watchdog = getattr(self, "_loop_watchdog", None)
        if _watchdog is not None:
            _watchdog.stop()
        self.md_service.running = False
        self.exec_service.running = False
        self.risk_engine.running = False
        self.recon_service.running = False

        # Drain RingBufferBus before stopping StrategyRunner consumer so that
        # events already published but not yet processed are not lost.
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
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Bus drain failed during shutdown", error=str(exc))

        self.strategy_runner.running = False
        self.execution_gateway.stop()  # Clean shutdown
        self.session_hook_manager.stop()
        self.health_server.stop()

        # Stop gateway service gracefully before task cancellation so its
        # finally block (dedup.persist()) runs while the event loop is live.
        if getattr(self, "gateway_service", None) is not None:
            self.gateway_service.running = False

        # I2-C2: Drain remaining fills from exec queue before order cancellation
        try:
            drained = await asyncio.wait_for(self.exec_service.stop(), timeout=3.0)
            if drained:
                logger.info("ExecutionRouter shutdown drain", fills_drained=drained)
        except asyncio.TimeoutError:
            logger.warning("ExecutionRouter drain timeout during shutdown")
        except Exception as exc:
            logger.warning("ExecutionRouter drain failed", error=str(exc))

        # Persist fill dedup window for restart-safe exactly-once fills
        try:
            # Let any checkpoint already queued on the executor finish first, so
            # the final synchronous write is not contending with it for the
            # persist lock while the loop is on its way down.
            await self.exec_service.flush_fill_dedup()
            self.exec_service.persist_fill_dedup()
        except Exception as exc:
            logger.warning("fill_dedup_persist_failed_shutdown", error=str(exc))

        # Persist orphaned fill DLQ so orphaned fills survive restart
        try:
            from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

            get_orphaned_fill_dlq().persist()
        except Exception as exc:
            logger.warning("fill_dlq_persist_failed_shutdown", error=str(exc))

        # H1: Drain in-flight orders and checkpoint positions before shutdown
        try:
            await asyncio.wait_for(self.order_adapter.drain_and_cancel(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Order drain timeout during shutdown")
        except Exception as exc:
            logger.warning("Order drain failed during shutdown", error=str(exc))

        # Persist order_id_map for restart-safe strategy resolution
        try:
            # Let any checkpoint already queued on the executor finish first,
            # so the final synchronous write is not contending with it for the
            # persist lock while the loop is on its way down.
            await self.order_adapter.flush_order_id_map()
            self.order_adapter.persist_order_id_map()
        except Exception as exc:
            logger.warning("order_id_map_persist_failed_shutdown", error=str(exc))

        if self.checkpoint_writer is not None:
            try:
                self.checkpoint_writer.write_checkpoint()
                logger.info("Final position checkpoint written")
            except Exception as exc:
                logger.warning("Final checkpoint failed", error=str(exc))

        # Stop AuditWriter flush tasks and drain remaining rows
        _aw = getattr(self, "_audit_writer", None)
        if _aw is not None:
            try:
                await _aw.stop()
                logger.info("AuditWriter stopped")
            except Exception as exc:
                logger.warning("AuditWriter stop failed", error=str(exc))

        # WU-01: Broker logout before task cancellation
        for cn in ("md_client", "order_client"):
            self._close_broker_client(cn)

        # Phase 1: Cancel recorder_bridge first so it stops enqueuing into recorder_queue.
        bridge_task = self.tasks.get("recorder_bridge")
        if bridge_task and not bridge_task.done():
            bridge_task.cancel()
            try:
                await asyncio.wait_for(bridge_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.error("recorder_bridge cleanup error", error=str(e))

        # Phase 2: Now signal recorder to drain remaining queue items and stop.
        if hasattr(self, "recorder") and self.recorder is not None:
            self.recorder.running = False

        # Phase 2b: Wait for recorder to finish its shutdown flush.
        # The recorder has its own 60s flush timeout (HFT_RECORDER_SHUTDOWN_TIMEOUT_S).
        # We allow it slightly more to account for the drain phase before flush.
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

        # Phase 3: Cancel and await all remaining tasks.
        for name, task in list(self.tasks.items()):
            if name in ("recorder_bridge", "recorder"):
                continue  # Already handled above
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Task cleanup timeout", task=name)
                except asyncio.CancelledError:
                    pass  # Expected
                except Exception as e:
                    logger.error("Task cleanup error", task=name, error=str(e))

        self.tasks.clear()
        self._teardown_bootstrap()

        # Opt-in: stop AutonomyMonitor first (it reacts to states, stop before governor)
        if self.autonomy_monitor is not None:
            try:
                await self.autonomy_monitor.stop()
            except Exception as exc:
                logger.warning("AutonomyMonitor stop failed", error=str(exc))

        # Opt-in: stop SessionGovernor after autonomy monitor
        if self.session_governor is not None:
            try:
                await self.session_governor.stop()
            except Exception as exc:
                logger.warning("SessionGovernor stop failed", error=str(exc))

        # Shutdown boundary; see the note at system_start. Left synchronous.
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
        # NOTE: Do NOT set recorder.running=False here. The recorder must stay
        # alive until _recorder_bridge (and any direct-write producers) have
        # stopped enqueuing. The async shutdown path (_async_stop / _cleanup_tasks)
        # cancels the bridge first, THEN signals the recorder to drain.
        self.execution_gateway.stop()  # Clean shutdown
        self.session_hook_manager.stop()
        self.health_server.stop()

        # Stop gateway service gracefully before broker logout/task cancellation
        # so its finally block (dedup.persist()) can complete.
        if getattr(self, "gateway_service", None) is not None:
            self.gateway_service.running = False

        # Schedule async cleanup if event loop is available.
        # H13: When the loop is running, defer broker close and bootstrap
        # teardown to stop_async() so recorder can drain first.
        # P1 fix: track the detached task on ``self._stop_async_task`` so the
        # launcher (main.py) can await it before the loop is torn down — fixes
        # fire-and-forget pattern where recorder drain / final checkpoint /
        # order drain were cut short by ``asyncio.run`` loop teardown.
        loop = getattr(self, "loop", None)
        if loop is not None and loop.is_running():
            self._stop_async_task = asyncio.create_task(self.stop_async())
        else:
            # Synchronous fallback: event loop not running.
            # Flush recorder data before teardown to prevent silent data loss
            # (INFRA-015). Use a temporary event loop for async drain.
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
        # This callback runs in Shioaji thread.
        # We must schedule work on the main loop.
        loop = getattr(self, "loop", None)
        from hft_platform.execution.normalizer import RawExecEvent

        # For deal callbacks, attempt to resolve strategy_id as early as possible.
        # Prefer strong correlation (broker IDs/custom_field token in order_id_map),
        # then fall back to the pending fill index only when necessary.
        if topic == "deal" and hasattr(self, "order_adapter") and self.order_adapter is not None:
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
            _resolved = None
            resolver = getattr(self.order_adapter, "order_id_resolver", None)
            if resolver is not None:
                _resolved = resolver.resolve_strategy_id_from_candidates([str(v) for v in _id_candidates if v])
                if _resolved == "UNKNOWN":
                    _resolved = None
            if _resolved is None and _action:
                _symbols = [str(v) for v in (_full_code, _code) if v]
                if _symbols:
                    _resolved = self.order_adapter.resolve_strategy_from_deal_candidates(_symbols, str(_action))
            if _resolved and isinstance(data, dict):
                data["_resolved_strategy_id"] = _resolved

        event = RawExecEvent(topic, data, timebase.now_ns())
        if not self.running:
            # Buffer for later drain instead of dropping — broker can send callbacks
            # before run() sets self.running = True.
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
                # Loop is closing/closed — fall through to overflow buffer
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
            # I-H4: loop not yet assigned (startup race) — buffer so events aren't dropped
            if len(self._exec_overflow_buf) >= self._EXEC_OVERFLOW_MAX:
                self._exec_overflow_evicted += 1
                try:
                    from hft_platform.observability.metrics import MetricsRegistry

                    MetricsRegistry.get().exec_overflow_evicted_total.inc()
                except Exception:
                    pass  # metrics may not be ready during early startup
                logger.critical(
                    "exec_overflow_buf FULL in broker thread — fill persisted to DLQ file",
                    evicted_count=self._exec_overflow_evicted,
                    event_topic=getattr(event, "topic", "?"),
                )
                self._persist_lost_exec_event(event)
                # Flag for deferred halt — checked when loop becomes available
                self._exec_startup_overflow_lost = True
                return
            self._exec_overflow_buf.append(event)

    async def _recorder_bridge(self):
        """Bridge all Bus events to Recorder."""
        # Safety guard: if all direct recording flags are set, this coroutine should not run.
        if self._md_record_direct and self._fill_record_direct and self._order_record_direct:
            logger.info(
                "recorder_bridge_early_exit",
                reason="all_direct_recording_enabled",
            )
            return
        # Start from -1 to capture first event
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
                    # Skip FillEvent/OrderEvent if direct recording is enabled (avoid duplicates)
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
