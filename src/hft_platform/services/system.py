import asyncio
import collections
import gc
import os
from typing import Any, Dict, Optional

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.core.session_hooks import SessionHookManager
from hft_platform.observability.health import HealthServer
from hft_platform.ops.evidence import get_shared_autonomy_evidence_writer
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.bootstrap import SystemBootstrapper
from hft_platform.services.heartbeat import write_heartbeat
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


class HFTSystem:
    # -- Typed helpers to replace hasattr probes ----------------------------------

    @staticmethod
    def _get_max_feed_gap_s(md_service: Any) -> float:
        """Return max feed gap from market data service, or 0.0 if unavailable."""
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
    def _set_service_running(service: Any, value: bool) -> None:
        """Set the ``running`` attribute on *service* if it exists."""
        if hasattr(service, "running"):
            service.running = value

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        configure_logging()
        self.settings = settings or {}
        self.running = False
        self._recorder_seen_tick = False
        self._recorder_seen_bidask = False
        self._md_record_direct = os.getenv("HFT_MD_RECORD_DIRECT", "1").lower() not in {"0", "false", "no", "off"}
        self._fill_record_direct = True  # Always use direct fill recording when recorder_queue is wired
        self._order_record_direct = True  # Always use direct order recording (H5: prevent ring buffer overwrite)

        self.bootstrapper = SystemBootstrapper(self.settings)
        self.registry = self.bootstrapper.build()

        self.bus = self.registry.bus
        self.raw_queue = self.registry.raw_queue
        self.raw_exec_queue = self.registry.raw_exec_queue
        self._exec_overflow_buf: collections.deque = collections.deque(maxlen=4096)
        self._EXEC_OVERFLOW_MAX: int = 4096
        self._exec_overflow_counter: int = 0
        self._exec_overflow_evicted: int = 0
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
        self.exec_service._overflow_buf = self._exec_overflow_buf
        self.risk_engine = self.registry.risk_engine
        self.recon_service = self.registry.recon_service
        self.strategy_runner = self.registry.strategy_runner
        self.recorder = self.registry.recorder
        self.gateway_service = self.registry.gateway_service
        self.intent_channel = getattr(self.registry, "intent_channel", None)
        self.checkpoint_writer = getattr(self.registry, "checkpoint_writer", None)
        self.startup_verifier = getattr(self.registry, "startup_verifier", None)
        self.session_governor = getattr(self.registry, "session_governor", None)
        self.autonomy_monitor = getattr(self.registry, "autonomy_monitor", None)
        self.daily_report_service = getattr(self.registry, "daily_report_service", None)
        self.evidence_writer = getattr(self.registry, "evidence_writer", None) or get_shared_autonomy_evidence_writer()
        self.platform_degrade_controller = (
            getattr(self.registry, "platform_degrade_controller", None) or get_shared_platform_degrade_controller()
        )
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
        self.order_adapter._storm_guard = self.storm_guard  # M1: live HALT check
        self.recon_service.platform_degrade_controller = self.platform_degrade_controller

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
        self._task_restart_base_delay_s = self._env_float("HFT_TASK_RESTART_BACKOFF_S", 1.0, min_value=0.1)
        self._task_restart_max_delay_s = self._env_float("HFT_TASK_RESTART_BACKOFF_MAX_S", 30.0, min_value=0.1)
        self._queue_log_every_s = self._env_float("HFT_SUPERVISOR_QUEUE_LOG_EVERY_S", 30.0, min_value=1.0)
        self._last_queue_log_s = 0.0
        self._recorder_bridge_drops: int = 0
        self._pnl_snapshot_drops: int = 0

        # WU-11: Session hooks (disabled by default)
        self.session_hook_manager = SessionHookManager()

        # WU-17: Structured health endpoint
        self.health_server = HealthServer(system=self)

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()
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

        # Login order_client (separate Shioaji session for execution).
        # md_client logs in via MarketDataService._connect_sequence(), but
        # order_client needs its own login for contract resolution + order callbacks.
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.order_client.login)
            logger.info("order_client logged in", contracts_ready=getattr(self.order_client, "contracts_ready", "N/A"))
        except Exception as exc:
            logger.error("order_client login failed — orders will be unavailable", error=str(exc))

        # Hooks for Shioaji
        self.order_client.set_execution_callbacks(
            on_order=lambda state, payload: self._on_exec("order", {"state": state, "payload": payload}),
            on_deal=lambda payload: self._on_exec("deal", {"payload": payload}),
        )

        try:
            # Opt-in: start SessionGovernor before market services
            if self.session_governor is not None:
                await self.session_governor.start()
                logger.info("SessionGovernor started")

            # Opt-in: start AutonomyMonitor via supervisor (so crashes are detected/restarted)
            if self.autonomy_monitor is not None:
                self._start_service("autonomy_monitor", self.autonomy_monitor.run())

            # Start Services
            # Recorder MUST start before exec_router to prevent fill recording gaps
            # during startup (fills can arrive as soon as execution callbacks are wired).
            self._start_service("recorder", self.recorder.run())
            self._start_service("md", self.md_service.run())
            self._start_service("exec_router", self.exec_service.run())
            # CE-M2: start GatewayService when enabled; otherwise start RiskEngine standalone
            if self.gateway_service is not None:
                self._start_service("gateway", self.gateway_service.run())
            else:
                self._start_service("risk", self.risk_engine.run())
            self._start_service("order", self.order_adapter.run())
            self._start_service("exec_gateway", self.execution_gateway.run())
            # ── Position Recovery (must complete before recon + strategy) ──
            if os.getenv("HFT_STARTUP_RECON_ENABLED", "1") == "1" and self.startup_verifier:
                try:
                    recovery = await self.startup_verifier.recover(
                        account_id=self.registry.broker_id,
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

            # ── Checkpoint Writer (after recovery, before trading) ──
            if os.getenv("HFT_CHECKPOINT_ENABLED", "1") == "1" and self.checkpoint_writer:
                self._start_service("checkpoint_writer", self.checkpoint_writer.run())

            self._start_service("recon", self.recon_service.run())
            self._start_service("strat", self.strategy_runner.run())
            if hasattr(self.strategy_runner, '_rejection_queue') and self.strategy_runner._rejection_queue is not None:
                self._start_service("rejection_consumer", self.strategy_runner._run_rejection_consumer())

            # Start AuditWriter flush tasks (singleton, lazy-created by RiskEngine/StormGuard)
            try:
                from hft_platform.recorder.audit import get_audit_writer
                self._audit_writer = get_audit_writer()
                await self._audit_writer.start()
                logger.info("AuditWriter started")
            except Exception as exc:
                logger.warning("AuditWriter start failed", error=str(exc))
                self._audit_writer = None
            if self._md_record_direct and self._fill_record_direct and self._order_record_direct:
                logger.info(
                    "recorder_bridge_skipped",
                    reason="all_direct_recording_enabled",
                )
            else:
                self._start_service("recorder_bridge", self._recorder_bridge())
            if os.getenv("HFT_PNL_EXPORTER_ENABLED", "1").lower() not in {"0", "false", "no", "off"}:
                self._start_service("pnl_exporter", self._pnl_snapshot_exporter())

            # WU-11: Session hooks
            if self.session_hook_manager.enabled:
                self._start_service("session_hooks", self.session_hook_manager.run())

            # WU-17: Structured health endpoint
            self._start_service("health_server", self.health_server.run())

            # Disable GC during active trading (HFT Core Law 1: Allocator Law)
            if os.getenv("HFT_GC_DISABLE_TRADING", "0").strip().lower() in {"1", "true", "yes", "on"}:
                gc.disable()
                _gc_disabled = True
                logger.info("GC disabled for trading session")

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
        services: list[tuple[str, str, Any]] = [
            ("md", "MarketDataService", self.md_service.run),
            ("exec_router", "ExecutionRouter", self.exec_service.run),
            ("order", "OrderAdapter", self.order_adapter.run),
            ("exec_gateway", "ExecutionGateway", self.execution_gateway.run),
            ("recon", "ReconciliationService", self.recon_service.run),
            ("strat", "StrategyRunner", self.strategy_runner.run),
            ("recorder", "RecorderService", self.recorder.run),
            *(
                [("recorder_bridge", "RecorderBridge", self._recorder_bridge)]
                if not (self._md_record_direct and self._fill_record_direct and self._order_record_direct)
                else []
            ),
            ("pnl_exporter", "PnLSnapshotExporter", self._pnl_snapshot_exporter),
        ]
        if self.gateway_service is not None:
            services.append(("gateway", "GatewayService", self.gateway_service.run))
        else:
            services.append(("risk", "RiskEngine", self.risk_engine.run))
        if self.autonomy_monitor is not None:
            services.append(("autonomy_monitor", "AutonomyMonitor", self.autonomy_monitor.run))
        return services

    def _reset_restart_backoff_if_healthy(self, name: str, task: asyncio.Task[Any] | None) -> None:
        if task and not task.done():
            self._task_restart_attempts.pop(name, None)
            self._task_restart_until_s.pop(name, None)

    def _try_restart_service(self, name: str, component: str, coro_factory: Any) -> None:
        now_s = timebase.now_s()
        allowed_at = self._task_restart_until_s.get(name, 0.0)
        if now_s < allowed_at:
            return
        attempt = self._task_restart_attempts.get(name, 0) + 1
        delay_s = min(self._task_restart_base_delay_s * (2 ** (attempt - 1)), self._task_restart_max_delay_s)
        self._task_restart_attempts[name] = attempt
        self._task_restart_until_s[name] = now_s + delay_s
        logger.warning(
            "Restarting service task",
            task=name,
            component=component,
            attempt=attempt,
            next_retry_after_s=round(delay_s, 2),
        )
        self._start_service(name, coro_factory())

    def _update_platform_degrade_state(self) -> None:
        controller = getattr(self, "platform_degrade_controller", None)
        inputs = getattr(self, "platform_degrade_inputs", None)
        if controller is None or inputs is None:
            return
        reasons = inputs.reduce_only_reasons()
        for reason in reasons:
            controller.enter_reduce_only(reason=reason)
        controller.check_auto_recovery(
            current_reasons=reasons,
            now_ns=timebase.now_ns(),
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
        _heartbeat_path = os.getenv("HFT_HEARTBEAT_PATH", "/tmp/hft-heartbeat")
        _heartbeat_interval_ticks = int(os.getenv("HFT_HEARTBEAT_INTERVAL_S", "30"))
        _heartbeat_tick = 0

        while self.running:
            await asyncio.sleep(interval_s)  # 1Hz Tick
            now_tick = loop.time()
            lag_s = max(0.0, now_tick - last_tick - interval_s)
            metrics.event_loop_lag_ms.set(lag_s * 1000.0)
            last_tick = now_tick

            # A. Update StormGuard with real metrics
            try:
                # 1. Get feed gap from market data service
                feed_gap_s = self._get_max_feed_gap_s(self.md_service)

                # 2. Get drawdown from position store (realized + unrealized)
                drawdown_pct = self._get_drawdown_pct(self.position_store, self.settings)
                if self._mtm_calculator is not None:
                    try:
                        unrealized = self._mtm_calculator.total_unrealized_pnl()
                        base_capital = self.settings.get("base_capital", 10_000_000)
                        if base_capital > 0 and unrealized < 0:
                            drawdown_pct = drawdown_pct + unrealized / base_capital
                    except Exception:
                        pass

                # 3. Get P99 latency estimate (convert event loop lag to microseconds as proxy)
                latency_us = int(lag_s * 1_000_000)

                # 3b. Inform StormGuard of session state to suppress feed-gap noise
                if self.session_governor is not None:
                    from hft_platform.ops.session_governor import SessionPhase
                    _ACTIVE_PHASES = frozenset({
                        SessionPhase.PRE_OPEN, SessionPhase.OPEN,
                        SessionPhase.CLOSE_ONLY, SessionPhase.FORCE_FLAT,
                    })
                    gate = getattr(self.session_governor, "track_gate", None)
                    if gate is not None:
                        phases = gate.track_phases
                        any_open = any(p in _ACTIVE_PHASES for p in phases.values())
                        self.storm_guard.set_session_active(any_open)

                # 4. Update StormGuard state (convert drawdown % to bps at boundary)
                drawdown_bps = int(drawdown_pct * 10_000)
                self.storm_guard.update(
                    drawdown_bps=drawdown_bps,
                    latency_us=latency_us,
                    feed_gap_s=feed_gap_s,
                )

                # 4b. Update StormGuard with LOB-derived drift-burst toxicity.
                # Feed only the primary symbol to DriftBurstDetector to avoid
                # cross-symbol contamination (M4 fix). Primary = first subscribed
                # symbol with an active book. For multi-symbol deployments, consider
                # per-symbol DriftBurstDetector instances.
                if hasattr(self.storm_guard, "update_with_lob"):
                    lob_engine = getattr(self.md_service, "lob", None)
                    if lob_engine is not None:
                        for _sym, book in lob_engine.books.items():
                            if book.mid_price_x2 > 0:
                                self.storm_guard.update_with_lob(
                                    mid_price_x2=book.mid_price_x2,
                                    spread_scaled=book.spread,
                                    imbalance=book.imbalance,
                                    ts=timebase.now_ns(),
                                )
                                break  # single detector: primary symbol only

                # 5. Update per-symbol feed gap metrics
                feed_gap_metric = getattr(metrics, "feed_gap_by_symbol_seconds", None)
                if feed_gap_metric is not None:
                    for symbol, gap in self._get_feed_gaps_by_symbol(self.md_service).items():
                        capped = metrics.cap_symbol(symbol) if metrics else symbol
                        feed_gap_metric.labels(symbol=capped).set(gap)

            except Exception as e:
                logger.warning("StormGuard update failed", error=str(e))

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
                        self._try_restart_service(name, component, coro_factory)
                    continue
                logger.critical(
                    "Critical service task stopped",
                    task=name,
                    component=component,
                    error=str(exc) if exc else "task_exited_without_exception",
                )
                self.storm_guard.trigger_halt(f"Critical Component Crash: {component}")
                if self.running:
                    self._try_restart_service(name, component, coro_factory)

            # Update Metrics — offload blocking psutil calls off the event loop
            await loop.run_in_executor(None, metrics.update_system_metrics)
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
            now_s = timebase.now_s()
            if now_s - self._last_queue_log_s >= self._queue_log_every_s:
                self._last_queue_log_s = now_s
                _gateway_intent_depth = (
                    getattr(self.intent_channel, "qsize", lambda: 0)()
                    if self.intent_channel is not None
                    else None
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
                logger.info("Queues", **_log_kwargs)

            self._update_platform_degrade_state()

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
                logger.error("System HALTED by StormGuard - blocking orders")
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
                # Defense-in-depth: propagate HALT to gateway policy so the gateway
                # rejects new intents independently of the risk engine path.
                # GatewayPolicy._set_mode() is idempotent, so repeated calls are safe.
                if self.gateway_service is not None:
                    self.gateway_service.set_halt()
                    logger.warning("Gateway policy set to HALT by StormGuard supervisor")
                # H6: Cancel in-flight orders already dispatched to broker
                try:
                    asyncio.create_task(self.order_adapter.drain_and_cancel())
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

    async def stop_async(self):
        """Async stop with proper task cleanup."""
        self.running = False
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

        # H1: Drain in-flight orders and checkpoint positions before shutdown
        try:
            await asyncio.wait_for(self.order_adapter.drain_and_cancel(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("Order drain timeout during shutdown")
        except Exception as exc:
            logger.warning("Order drain failed during shutdown", error=str(exc))

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

        # Phase 3: Cancel and await all remaining tasks.
        for name, task in list(self.tasks.items()):
            if name == "recorder_bridge":
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

        for cn in ("md_client", "order_client"):
            self._close_broker_client(cn)
        self._teardown_bootstrap()
        self.evidence_writer.record_transition(
            scope="platform",
            mode="CLOSED",
            reason="system_stop",
            manual_rearm_required=False,
        )

        # Schedule async cleanup if event loop is available
        loop = getattr(self, "loop", None)
        if loop is not None and loop.is_running():
            asyncio.create_task(self._cleanup_tasks())

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
                    "exec_overflow_buf FULL — fill LOST",
                    evicted_count=self._exec_overflow_evicted,
                    event_topic=getattr(event, "topic", "?"),
                )
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
        if not self.running:
            return
        from hft_platform.execution.normalizer import RawExecEvent

        event = RawExecEvent(topic, data, timebase.now_ns())
        if loop is not None:
            loop.call_soon_threadsafe(self._safe_enqueue_exec, event)
        else:
            # I-H4: loop not yet assigned (startup race) — buffer so events aren't dropped
            if len(self._exec_overflow_buf) >= self._EXEC_OVERFLOW_MAX:
                self._exec_overflow_evicted += 1
                logger.critical(
                    "exec_overflow_buf FULL in broker thread — fill LOST",
                    evicted_count=self._exec_overflow_evicted,
                    event_topic=getattr(event, "topic", "?"),
                )
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
            self.bus.consume_batch(batch_size, start_cursor=-1) if batch_size > 1 else self.bus.consume(start_cursor=-1)
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
