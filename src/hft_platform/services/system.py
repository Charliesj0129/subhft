import asyncio
from typing import Any, Dict, Optional

from structlog import get_logger

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.worker import RecorderService
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard, StormGuardState
from hft_platform.services.execution import ExecutionService
from hft_platform.services.market_data import MarketDataService
from hft_platform.strategy.runner import StrategyRunner
from hft_platform.utils.logging import configure_logging

logger = get_logger("system")


class HFTSystem:
    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        configure_logging()
        self.settings = settings or {}
        self.running = False

        # 1. Infrastructure
        self.bus = RingBufferBus()
        self.raw_queue = asyncio.Queue()
        self.raw_exec_queue = asyncio.Queue()
        self.risk_queue = asyncio.Queue()
        self.order_queue = asyncio.Queue()

        # 2. Shared State
        self.position_store = PositionStore()
        self.order_id_map: Dict[str, str] = {}
        self.storm_guard = StormGuard()

        # 3. Config Paths
        import os
        paths = self.settings.get("paths", {})
        symbols_path = os.getenv("SYMBOLS_CONFIG", paths.get("symbols", "config/symbols.yaml"))
        risk_path = paths.get("strategy_limits", "config/base/strategy_limits.yaml")
        adapter_path = paths.get("order_adapter", "config/base/order_adapter.yaml")

        # Inject StormGuard into Client? Or just System manages access?
        # Ideally components check StormGuard via shared state or singleton?
        # For now, System pushes state or components access generic registry.

        self.client = ShioajiClient(symbols_path)

        # 4. Services
        self.md_service = MarketDataService(self.bus, self.raw_queue, self.client)

        # Pass OrderAdapter logic (Legacy component)
        self.order_adapter = OrderAdapter(adapter_path, self.order_queue, self.client, self.order_id_map)
        # Inject StormGuard reference to Adapter if possible, or Adapter uses System global?
        # Better: We'll modify Adapter later to accept it. For now, leave as is.

        self.exec_service = ExecutionService(
            self.bus, self.raw_exec_queue, self.order_id_map, self.position_store, self.order_adapter
        )

        self.risk_engine = RiskEngine(risk_path, self.risk_queue, self.order_queue)

        self.recon_service = ReconciliationService(self.client, self.position_store, self.settings)

        # LOB is owned by MD Service, pass it to Runner
        self.strategy_runner = StrategyRunner(self.bus, self.risk_queue, self.md_service.lob, self.position_store)

        # 5. Recorder
        self.recorder_queue = asyncio.Queue()
        self.recorder = RecorderService(self.recorder_queue)

        self.tasks: Dict[str, asyncio.Task] = {}

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()
        logger.info("System Starting...")

        # Hooks for Shioaji
        self.client.set_execution_callbacks(
            on_order=lambda s, o: self._on_exec("order", {"status": s, "order": o}),
            on_deal=lambda d: self._on_exec("deal", d),
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
            await asyncio.sleep(1.0) # 1Hz Tick

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
        self.order_adapter.running = False # Clean shutdown

    async def _on_exec(self, topic, data):
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
        try:
            async for event in consumer:
                # Log sampling
                if "Tick" in str(type(event)):
                     logger.info(f"Bridge received Tick: {event}")

                 # Naive dump of everything to market_data table?
                # Ideally we check event type.
                # But for now, dump all after converting to dict
                # Assuming event is Pydantic model
                payload = event
                if hasattr(event, "dict"):
                    payload = event.dict()
                elif hasattr(event, "model_dump"):
                    payload = event.model_dump()

                await self.recorder_queue.put({"topic": "market_data", "data": payload})
        except asyncio.CancelledError:
            pass
