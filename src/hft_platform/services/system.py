import asyncio
import os
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
        self.client = self.registry.client
        self.symbol_metadata = self.registry.symbol_metadata
        self.price_scale_provider = self.registry.price_scale_provider

        self.md_service = self.registry.md_service
        self.order_adapter = self.registry.order_adapter
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
        self.client.set_execution_callbacks(
            on_order=lambda state, payload: self._on_exec("order", {"state": state, "payload": payload}),
            on_deal=lambda payload: self._on_exec("deal", {"payload": payload}),
        )

        try:
            # Start Services
            self._start_service("md", self.md_service.run())
            self._start_service("exec", self.exec_service.run())
            self._start_service("risk", self.risk_engine.run())
            self._start_service("order", self.order_adapter.run())
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

        while self.running:
            await asyncio.sleep(1.0)  # 1Hz Tick

            # A. Update StormGuard
            # usages = self.client.get_usage() # API Call
            # For prototype, we mock latency/drawdown inputs or read from metrics
            # Assuming LatencyMonitor writes to metrics, we skip reading them back here for now.
            # We assume StormGuard is updated via events or direct calls.
            # But here we act as the heartbeat.

            # Check Health
            # If OrderAdapter crashed
            t_order = self.tasks.get("order")
            if t_order and t_order.done():
                # Crash detected!
                try:
                    exc = t_order.exception()
                    logger.critical("OrderAdapter Crashed!", error=str(exc))

                    # Policy: Restart if not HALT?
                    # Trigger StormGuard HALT first for safety
                    self.storm_guard.trigger_halt("Critical Component Crash: OrderAdapter")

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
                logger.warning("Restarting OrderAdapter...")
                self._start_service("order", self.order_adapter.run())

            # Update Metrics
            metrics.update_system_metrics()
            if metrics:
                exec_task = self.tasks.get("exec")
                order_task = self.tasks.get("order")
                metrics.execution_router_alive.set(1 if exec_task and not exec_task.done() else 0)
                metrics.execution_gateway_alive.set(1 if order_task and not order_task.done() else 0)
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
        self.order_adapter.running = False  # Clean shutdown

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
        consumer = self.bus.consume(start_cursor=-1)
        from hft_platform.recorder.mapper import map_event_to_record

        metadata = self.symbol_metadata
        price_codec = PriceCodec(self.price_scale_provider)
        try:
            async for event in consumer:
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
