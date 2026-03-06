import asyncio
import os
from typing import Any, Dict, Optional

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.bootstrap import SystemBootstrapper
from hft_platform.utils.logging import configure_logging

logger = get_logger("system")


class HFTSystem:
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

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()
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
            except Exception:
                pass
        self.tasks[name] = asyncio.create_task(coro)

    @staticmethod
    def _env_float(name: str, default: float, min_value: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except Exception:
            value = default
        return max(min_value, value)

    def _teardown_bootstrap(self) -> None:
        if self._bootstrap_torn_down:
            return
        self._bootstrap_torn_down = True
        try:
            self.bootstrapper.teardown()
        except Exception as exc:
            logger.warning("Bootstrap teardown failed", error=str(exc))

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
                feed_gap_s = 0.0
                if hasattr(self.md_service, "get_max_feed_gap_s"):
                    feed_gap_s = self.md_service.get_max_feed_gap_s()
                    if hasattr(self.md_service, "within_reconnect_window"):
                        if not self.md_service.within_reconnect_window():
                            feed_gap_s = 0.0

                # 2. Get drawdown from position store
                drawdown_pct = 0.0
                if hasattr(self.position_store, "get_drawdown_pct"):
                    drawdown_pct = self.position_store.get_drawdown_pct()
                elif hasattr(self.position_store, "total_pnl"):
                    # Simple drawdown approximation from PnL
                    total_pnl = self.position_store.total_pnl
                    if total_pnl < 0:
                        # Assume a base capital for percentage calculation
                        base_capital = self.settings.get("base_capital", 10_000_000)
                        drawdown_pct = total_pnl / base_capital if base_capital > 0 else 0.0

                # 3. Get P99 latency estimate (convert event loop lag to microseconds as proxy)
                latency_us = int(lag_s * 1_000_000)

                # 4. Update StormGuard state
                self.storm_guard.update(
                    drawdown_pct=drawdown_pct,
                    latency_us=latency_us,
                    feed_gap_s=feed_gap_s,
                )

                # 5. Update per-symbol feed gap metrics
                has_gaps_method = hasattr(self.md_service, "get_feed_gaps_by_symbol")
                has_metric = hasattr(metrics, "feed_gap_by_symbol_seconds")
                if has_gaps_method and has_metric:
                    for symbol, gap in self.md_service.get_feed_gaps_by_symbol().items():
                        metrics.feed_gap_by_symbol_seconds.labels(symbol=symbol).set(gap)

            except Exception as e:
                logger.warning("StormGuard update failed", error=str(e))

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
                if hasattr(self.order_adapter, "running"):
                    self.order_adapter.running = False

    async def stop_async(self):
        """Async stop with proper task cleanup."""
        self.running = False
        self.md_service.running = False
        self.exec_service.running = False
        self.risk_engine.running = False
        self.recon_service.running = False
        self.strategy_runner.running = False
        self.execution_gateway.stop()  # Clean shutdown

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
        self._teardown_bootstrap()

        # Schedule async cleanup if event loop is available
        if hasattr(self, "loop") and self.loop.is_running():
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
        if self.running and hasattr(self, "loop"):
            from hft_platform.execution.normalizer import RawExecEvent

            event = RawExecEvent(topic, data, timebase.now_ns())
            self.loop.call_soon_threadsafe(self.raw_exec_queue.put_nowait, event)

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
