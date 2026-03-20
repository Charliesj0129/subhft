import asyncio
import os
from typing import Any, Dict, Optional

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.core.session_hooks import SessionHookManager
from hft_platform.observability.health import HealthServer
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.bootstrap import SystemBootstrapper
from hft_platform.utils.logging import configure_logging

logger = get_logger("system")


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

        self.bootstrapper = SystemBootstrapper(self.settings)
        self.registry = self.bootstrapper.build()

        self.bus = self.registry.bus
        self.raw_queue = self.registry.raw_queue
        self.raw_exec_queue = self.registry.raw_exec_queue
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
        self.risk_engine = self.registry.risk_engine
        self.recon_service = self.registry.recon_service
        self.strategy_runner = self.registry.strategy_runner
        self.recorder = self.registry.recorder
        self.gateway_service = self.registry.gateway_service

        self._mtm_calculator = None
        try:
            from hft_platform.execution.mtm import MarkToMarketCalculator

            lob_engine = getattr(self.md_service, "lob", None)
            if lob_engine is not None:
                self._mtm_calculator = MarkToMarketCalculator(
                    self.position_store, mid_price_fn=getattr(lob_engine, "get_mid_price", lambda s: None)
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

        # WU-11: Session hooks (disabled by default)
        self.session_hook_manager = SessionHookManager()

        # WU-17: Structured health endpoint
        self.health_server = HealthServer(system=self)

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()

        import signal

        try:
            self.loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        except (NotImplementedError, OSError):
            pass

        logger.info("System Starting...")

        # Hooks for Shioaji
        self.order_client.set_execution_callbacks(
            on_order=lambda state, payload: self._on_exec("order", {"state": state, "payload": payload}),
            on_deal=lambda payload: self._on_exec("deal", {"payload": payload}),
        )

        try:
            # Start Services
            self._start_service("md", self.md_service.run())
            self._start_service("exec_router", self.exec_service.run())
            # CE-M2: start GatewayService when enabled; otherwise start RiskEngine standalone
            if self.gateway_service is not None:
                self._start_service("gateway", self.gateway_service.run())
            else:
                self._start_service("risk", self.risk_engine.run())
            self._start_service("order", self.order_adapter.run())
            self._start_service("exec_gateway", self.execution_gateway.run())
            self._start_service("recon", self.recon_service.run())
            self._start_service("strat", self.strategy_runner.run())
            self._start_service("recorder", self.recorder.run())
            self._start_service("recorder_bridge", self._recorder_bridge())
            if os.getenv("HFT_PNL_EXPORTER_ENABLED", "1").lower() not in {"0", "false", "no", "off"}:
                self._start_service("pnl_exporter", self._pnl_snapshot_exporter())

            # WU-11: Session hooks
            if self.session_hook_manager.enabled:
                self._start_service("session_hooks", self.session_hook_manager.run())

            # WU-17: Structured health endpoint
            self._start_service("health_server", self.health_server.run())

            # Start Monitor/Supervisor Loop
            await self._supervise()

        except asyncio.CancelledError:
            logger.info("System Stopping...")
        finally:
            self.stop()

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
                        pass
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
            ("recorder_bridge", "RecorderBridge", self._recorder_bridge),
            ("pnl_exporter", "PnLSnapshotExporter", self._pnl_snapshot_exporter),
        ]
        if self.gateway_service is not None:
            services.append(("gateway", "GatewayService", self.gateway_service.run))
        else:
            services.append(("risk", "RiskEngine", self.risk_engine.run))
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

                # 4. Update StormGuard state (convert drawdown % to bps at boundary)
                drawdown_bps = int(drawdown_pct * 10_000)
                self.storm_guard.update(
                    drawdown_bps=drawdown_bps,
                    latency_us=latency_us,
                    feed_gap_s=feed_gap_s,
                )

                # 5. Update per-symbol feed gap metrics
                feed_gap_metric = getattr(metrics, "feed_gap_by_symbol_seconds", None)
                if feed_gap_metric is not None:
                    for symbol, gap in self._get_feed_gaps_by_symbol(self.md_service).items():
                        feed_gap_metric.labels(symbol=symbol).set(gap)

            except Exception as e:
                logger.warning("StormGuard update failed", error=str(e))

            # Kill-switch file check
            kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
            if os.path.exists(kill_switch_path):
                if self.storm_guard.state != StormGuardState.HALT:
                    try:
                        import json as _json

                        with open(kill_switch_path, "r") as _ksf:
                            _ks_data = _json.load(_ksf)
                        _ks_reason = _ks_data.get("reason", "unknown")
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
                if exc is None and name == "order" and self.storm_guard.state == StormGuardState.HALT:
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

            # Update Metrics
            metrics.update_system_metrics()
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
            now_s = timebase.now_s()
            if now_s - self._last_queue_log_s >= self._queue_log_every_s:
                self._last_queue_log_s = now_s
                logger.info(
                    "Queues",
                    raw=self.raw_queue.qsize(),
                    rec=self.recorder_queue.qsize(),
                    risk=self.risk_queue.qsize(),
                    order=self.order_queue.qsize(),
                    raw_exec=self.raw_exec_queue.qsize(),
                )

            now = timebase.now_s()
            t_router = self.tasks.get("exec_router")
            if t_router and not t_router.done():
                metrics.execution_router_heartbeat_ts.set(now)
            if t_gateway and not t_gateway.done():
                metrics.execution_gateway_heartbeat_ts.set(now)

            # Check StormGuard State - CRITICAL: Block orders when HALT
            if self.storm_guard.state == StormGuardState.HALT:
                logger.error("System HALTED by StormGuard - blocking orders")
                # Drain order queue to prevent any pending orders from executing
                drained_count = 0
                while not self.order_queue.empty():
                    try:
                        self.order_queue.get_nowait()
                        self.order_queue.task_done()
                        drained_count += 1
                    except asyncio.QueueEmpty:
                        break
                if drained_count > 0:
                    logger.warning("Drained blocked orders during HALT", count=drained_count)
                # Signal order adapter to stop processing
                self._set_service_running(self.order_adapter, False)

    async def stop_async(self):
        """Async stop with proper task cleanup."""
        self.running = False
        self.md_service.running = False
        self.exec_service.running = False
        self.risk_engine.running = False
        self.recon_service.running = False
        self.strategy_runner.running = False
        self.execution_gateway.stop()  # Clean shutdown
        self.session_hook_manager.stop()
        self.health_server.stop()

        # WU-01: Broker logout before task cancellation
        for cn in ("md_client", "order_client"):
            self._close_broker_client(cn)

        # Cancel and await all tasks for clean shutdown
        for name, task in list(self.tasks.items()):
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
        logger.info("System stopped and tasks cleaned up")

    def stop(self):
        """Synchronous stop (schedules async cleanup if loop is running)."""
        self.running = False
        self.md_service.running = False
        self.exec_service.running = False
        self.risk_engine.running = False
        self.recon_service.running = False
        self.strategy_runner.running = False
        self.execution_gateway.stop()  # Clean shutdown
        self.session_hook_manager.stop()
        self.health_server.stop()
        for cn in ("md_client", "order_client"):
            self._close_broker_client(cn)
        self._teardown_bootstrap()

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

    def _on_exec(self, topic, data):
        # This callback runs in Shioaji thread.
        # We must schedule work on the main loop.
        loop = getattr(self, "loop", None)
        if self.running and loop is not None:
            from hft_platform.execution.normalizer import RawExecEvent

            event = RawExecEvent(topic, data, timebase.now_ns())
            loop.call_soon_threadsafe(self.raw_exec_queue.put_nowait, event)

    async def _recorder_bridge(self):
        """Bridge all Bus events to Recorder."""
        # Start from -1 to capture first event
        batch_size = int(os.getenv("HFT_BUS_BATCH_SIZE", "0") or "0")
        consumer = (
            self.bus.consume_batch(batch_size, start_cursor=-1) if batch_size > 1 else self.bus.consume(start_cursor=-1)
        )
        from hft_platform.events import BidAskEvent, TickEvent
        from hft_platform.recorder.mapper import map_event_to_record

        metadata = self.symbol_metadata
        price_codec = PriceCodec(self.price_scale_provider)
        try:
            async for item in consumer:
                batch = item if isinstance(item, list) else [item]
                for event in batch:
                    if self._md_record_direct and isinstance(event, (TickEvent, BidAskEvent)):
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
                            pass
                    else:
                        await self.recorder_queue.put({"topic": topic, "data": payload})
        except asyncio.CancelledError:
            pass
