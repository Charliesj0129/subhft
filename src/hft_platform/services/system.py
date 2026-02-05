import asyncio
import os
import time
from typing import Any, Dict, Optional

from structlog import get_logger

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

        self.registry = SystemBootstrapper(self.settings).build()

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

        self.tasks: Dict[str, asyncio.Task[Any]] = {}
        self._recorder_drop_on_full = os.getenv("HFT_RECORDER_DROP_ON_FULL", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

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
        self.tasks[name] = asyncio.create_task(coro)

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

            # Check Health
            # If ExecutionGateway crashed
            t_gateway = self.tasks.get("exec_gateway")
            t_order = self.tasks.get("order")
            if t_gateway and t_gateway.done():
                # Crash detected!
                try:
                    exc = t_gateway.exception()
                    logger.critical("ExecutionGateway Crashed!", error=str(exc))

                    # Policy: Restart if not HALT?
                    # Trigger StormGuard HALT first for safety
                    self.storm_guard.trigger_halt("Critical Component Crash: ExecutionGateway")

                    # If policy allows restart?
                    # For now, we just log.
                    # To fix "Zombie Mode", we simply ensure we don't ignore it.
                    # We might want to restart:
                    # logger.info("Attempting Restart of OrderAdapter...")
                    # self._start_service("order", self.order_adapter.run())

                    # NOTE: Chaos Test expects "System Restart" or graceful handling.
                    # If we just leave it crashed, trading stops (Ghost).
                    # Let's Implement Restart.

                except asyncio.CancelledError:
                    pass

                # Restart logic
                logger.warning("Restarting ExecutionGateway...")
                self._start_service("exec_gateway", self.execution_gateway.run())

            if t_order and t_order.done():
                try:
                    exc = t_order.exception()
                    logger.critical("OrderAdapter Crashed!", error=str(exc))
                    self.storm_guard.trigger_halt("Critical Component Crash: OrderAdapter")
                except asyncio.CancelledError:
                    pass
                logger.warning("Restarting OrderAdapter...")
                self._start_service("order", self.order_adapter.run())

            # Update Metrics
            metrics.update_system_metrics()
            if metrics:
                exec_task = self.tasks.get("exec_router")
                gateway_task = self.tasks.get("exec_gateway")
                metrics.execution_router_alive.set(1 if exec_task and not exec_task.done() else 0)
                metrics.execution_gateway_alive.set(1 if gateway_task and not gateway_task.done() else 0)
                metrics.queue_depth.labels(queue="raw").set(self.raw_queue.qsize())
                metrics.queue_depth.labels(queue="recorder").set(self.recorder_queue.qsize())
                metrics.queue_depth.labels(queue="risk").set(self.risk_queue.qsize())
                metrics.queue_depth.labels(queue="order").set(self.order_queue.qsize())
                metrics.queue_depth.labels(queue="exec").set(self.raw_exec_queue.qsize())
            logger.info(
                "Queues",
                raw=self.raw_queue.qsize(),
                rec=self.recorder_queue.qsize(),
                risk=self.risk_queue.qsize(),
            )
            metrics.queue_depth.labels(queue="raw").set(self.raw_queue.qsize())
            metrics.queue_depth.labels(queue="raw_exec").set(self.raw_exec_queue.qsize())
            metrics.queue_depth.labels(queue="risk").set(self.risk_queue.qsize())
            metrics.queue_depth.labels(queue="order").set(self.order_queue.qsize())
            metrics.queue_depth.labels(queue="recorder").set(self.recorder_queue.qsize())

            now = time.time()
            t_router = self.tasks.get("exec_router")
            if t_router and not t_router.done():
                metrics.execution_router_heartbeat_ts.set(now)
            if t_gateway and not t_gateway.done():
                metrics.execution_gateway_heartbeat_ts.set(now)

            # Check StormGuard State
            if self.storm_guard.state == StormGuardState.HALT:
                logger.error("System HALTED by StormGuard.")
                # potentially self.stop() or just block orders?

    def stop(self):
        self.running = False
        self.md_service.running = False
        self.exec_service.running = False
        self.risk_engine.running = False
        self.recon_service.running = False
        self.strategy_runner.running = False
        self.execution_gateway.stop()  # Clean shutdown

    def _on_exec(self, topic, data):
        # This callback runs in Shioaji thread.
        # We must schedule work on the main loop.
        if self.running and hasattr(self, "loop"):
            import time

            from hft_platform.execution.normalizer import RawExecEvent

            event = RawExecEvent(topic, data, time.time_ns())
            self.loop.call_soon_threadsafe(self.raw_exec_queue.put_nowait, event)

    async def _recorder_bridge(self):
        """Bridge all Bus events to Recorder."""
        # Start from -1 to capture first event
        batch_size = int(os.getenv("HFT_BUS_BATCH_SIZE", "0") or "0")
        consumer = (
            self.bus.consume_batch(batch_size, start_cursor=-1) if batch_size > 1 else self.bus.consume(start_cursor=-1)
        )
        from hft_platform.recorder.mapper import map_event_to_record

        metadata = self.symbol_metadata
        price_codec = PriceCodec(self.price_scale_provider)
        try:
            async for item in consumer:
                batch = item if isinstance(item, list) else [item]
                for event in batch:
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
