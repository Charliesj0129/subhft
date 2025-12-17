import asyncio
import signal
import time
from importlib import import_module
from typing import Any, Dict, Optional

from structlog import get_logger

from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.utils.logging import configure_logging

from hft_platform.strategy.runner import StrategyRunner
from hft_platform.risk.engine import RiskEngine
from hft_platform.order.adapter import OrderAdapter
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.recorder.worker import RecorderService

configure_logging()
logger = get_logger("main")

class HFTSystem:
    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}
        self.bus = RingBufferBus()
        self.lob_engine = LOBEngine()
        self.normalizer = MarketDataNormalizer()
        symbols_path = self.settings.get("paths", {}).get("symbols", "config/symbols.yaml")
        self.client = ShioajiClient(symbols_path)

        self.order_id_map: Dict[str, str] = {}  # Shared lookup: BrokerID -> StrategyID

        # Queues
        self.risk_queue = asyncio.Queue()
        self.order_queue = asyncio.Queue()
        self.raw_queue = asyncio.Queue()  # Intermediate queue for callback discipline
        self.raw_exec_queue = asyncio.Queue()
        self.recorder_queue = asyncio.Queue()

        # State holders created early for downstream dependencies
        self.position_store = PositionStore()

        # Components
        strategy_limits_path = self.settings.get("paths", {}).get("strategy_limits", "config/strategy_limits.yaml")
        order_adapter_path = self.settings.get("paths", {}).get("order_adapter", "config/order_adapter.yaml")

        self.strategy_runner = StrategyRunner(self.bus, self.risk_queue, self.lob_engine, self.position_store)
        self.risk_engine = RiskEngine(strategy_limits_path, self.risk_queue, self.order_queue)
        self.order_adapter = OrderAdapter(order_adapter_path, self.order_queue, self.client, self.order_id_map)
        self.exec_normalizer = ExecutionNormalizer(self.raw_exec_queue, self.order_id_map)
        self.recon_service = ReconciliationService(self.client, self.position_store, {"reconciliation": {"heartbeat_threshold_ms": 1000}})
        self.recorder_service = RecorderService(self.recorder_queue)

        # Feed Adapter
        from hft_platform.feed_adapter.adapter import FeedAdapter

        self.feed_adapter = FeedAdapter(
            client=self.client,
            lob_engine=self.lob_engine,
            bus=self.bus,
            raw_queue=self.raw_queue,
            normalizer=self.normalizer,
        )

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.running = False

        self._load_strategy_from_settings()

    def _load_strategy_from_settings(self):
        spec = self.settings.get("strategy")
        if not spec:
            return
        try:
            mod = import_module(spec["module"])
            cls = getattr(mod, spec["class"])
            strat = cls(strategy_id=spec["id"], **(spec.get("params") or {}))
            self.strategy_runner.strategies = [strat]
            logger.info("Loaded strategy from settings", id=spec["id"])
        except Exception as exc:
            logger.error("Failed to load strategy from settings", error=str(exc))

    def on_shioaji_event(self, exchange, item):
        """
        Callback from Shioaji thread. 
        DISCIPLINE: Only capture time, (decode if simple), and enqueue.
        Must return immediately.
        """
        try:
            # 1. Capture Local Time (done inside normalizer usually, but here we just pass raw)
            # 2. Enqueue to asyncio loop
            if self.loop and self.running:
                self.loop.call_soon_threadsafe(self.raw_queue.put_nowait, item)
        except Exception as e:
            # Logging here might be too heavy? minimal print?
            pass

    async def process_raw_events(self):
        """
        Pinned consumer task: Dequeue raw -> Normalize -> Update LOB -> Publish
        """
        logger.info("Starting Raw Event Processor")
        while self.running:
            item = await self.raw_queue.get()
            try:
                # Detect type and normalize (now on consumer thread)
                # Simulated item as dict for this prototype
                normalized = None
                if "Close" in item or "close" in item: 
                    normalized = self.normalizer.normalize_tick(item)
                elif "BidPrice" in item or "bid_price" in item: 
                    normalized = self.normalizer.normalize_bidask(item)
                
                if normalized:
                    # 1. Update LOB & Get Stats
                    stats = self.lob_engine.process_event(normalized)
                    
                    # 2. Push to Bus
                    await self.bus.publish(normalized)
                    if stats:
                        await self.bus.publish(stats)
                    
            except Exception as e:
                logger.error("Error processing event", error=str(e))
            finally:
                self.raw_queue.task_done()

    async def bootstrap(self):
        logger.info("Starting Bootstrap...")
        self.client.login() 
        
        snapshots = self.client.fetch_snapshots()
        for snap in snapshots:
            pass
        logger.info("Snapshots applied", count=len(snapshots))

        def _cb(exchange, item):
            self.on_shioaji_event(exchange, item)
            
        self.client.subscribe_basket(_cb)
        logger.info("Subscriptions active")

    async def monitor_usage(self):
        """Periodic usage polling (T8) & System Metrics (T10)."""
        from hft_platform.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry.get()
        while self.running:
            usage = self.client.get_usage()
            logger.debug("Traffic Usage", **usage)

            # Update System Metrics
            metrics.update_system_metrics()

            await asyncio.sleep(15)


    def on_execution_event(self, topic: str, data: Dict[str, Any]):
        """
        Callback from Shioaji thread (Orders/Deals).
        """
        try:
             # Capture Time
             ts = time.time_ns()
             event = RawExecEvent(topic, data, ts)
             if self.loop and self.running:
                 self.loop.call_soon_threadsafe(self.raw_exec_queue.put_nowait, event)
        except Exception:
             pass

    async def process_execution_events(self):
        logger.info("Starting Execution Processor")
        while self.running:
            try:
                raw = await self.raw_exec_queue.get()
                
                # Normalize
                if raw.topic == "order":
                    norm = self.exec_normalizer.normalize_order(raw)
                    if norm:
                        await self.bus.publish(norm) # Strategy sees order update
                        
                        # Cleanup OrderAdapter if terminal
                        try:
                            # OrderStatus enum values: FILLED=3, CANCELLED=4, FAILED=5
                            # Assuming norm.status is IntEnum or int
                            if norm.status >= 3: # FILLED, CANCELLED, FAILED
                                self.order_adapter.on_terminal_state(norm.strategy_id, norm.order_id)
                        except Exception as e:
                            logger.error("Failed to clean up order", error=str(e))
                
                elif raw.topic == "deal":
                    norm = self.exec_normalizer.normalize_fill(raw)
                    if norm:
                        # Update Position
                        delta = self.position_store.on_fill(norm)
                        await self.bus.publish(delta) # Strategy/Risk sees position change
                        await self.bus.publish(norm)  # Strategy sees fill details
                
                self.raw_exec_queue.task_done()
            except Exception as e:
                logger.error("Exec processing error", error=str(e))


    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()

        # Hooks
        self.client.set_execution_callbacks(
            on_order=lambda status, order: self.on_execution_event("order", {"status": status, "order": order}),
            on_deal=lambda deal: self.on_execution_event("deal", deal)
        )

        # Start Consumers
        feed_task = asyncio.create_task(self.feed_adapter.run())
        exec_task = asyncio.create_task(self.process_execution_events())
        monitor_task = asyncio.create_task(self.monitor_usage())

        # Start Strategy/Risk/Order tasks
        strat_task = asyncio.create_task(self.strategy_runner.run())

        if not self.strategy_runner.strategies:
            logger.warning("No strategies loaded. Verify config/strategies.yaml.")
        
        risk_task = asyncio.create_task(self.risk_engine.run())
        order_task = asyncio.create_task(self.order_adapter.run())
        recon_task = asyncio.create_task(self.recon_service.run())
        recorder_task = asyncio.create_task(self.recorder_service.run())

        # Bridge Bus -> Recorder Queue
        # Bridge Bus -> Recorder Queue
        async def bus_to_recorder():
             import time
             from hft_platform.utils.serialization import serialize

             async for event in self.bus.consume():
                 try:
                     # Unified Schema: {topic: str, data: dict, ts: int}
                     topic = "unknown"
                     data_obj = event
                     ts = time.time_ns()

                     # 1. Determine Topic & Payload
                     if isinstance(event, dict):
                         # Market Data
                         etype = event.get("type")
                         if etype in ["Tick", "BidAsk", "Snapshot"]:
                             topic = "market_data"
                         elif "topic" in event:
                             topic = event["topic"]
                         
                         data_obj = event
                     else:
                         # Objects
                         cname = event.__class__.__name__
                         if cname == "FillEvent":
                             topic = "fills"
                         elif cname == "OrderEvent":
                             topic = "orders"
                         elif cname == "OrderIntent":
                             topic = "risk_log" # Capture risk intents if published?
                         elif hasattr(event, "topic"):
                             topic = getattr(event, "topic")
                         else:
                             topic = cname.lower()
                         
                         if hasattr(event, "ingest_ts_ns"):
                             ts = event.ingest_ts_ns
                         elif hasattr(event, "timestamp_ns"):
                             ts = event.timestamp_ns
                         
                         data_obj = event

                     # 2. Serialize Payload
                     serialized_data = serialize(data_obj)
                     
                     # 3. Construct Record
                     record = {
                         "topic": topic,
                         "data": serialized_data,
                         "ts": ts
                     }
                     
                     await self.recorder_queue.put(record)
                 except Exception:
                     pass # robust

        recorder_bridge_task = asyncio.create_task(bus_to_recorder())

        # FeedAdapter handles bootstrap now
        # await self.bootstrap() <- REMOVE logic from main

        logger.info("Entering main loop")
        tasks = [
            feed_task, 
            exec_task, 
            monitor_task, 
            strat_task, 
            risk_task, 
            order_task, 
            recon_task, 
            recorder_task, 
            recorder_bridge_task
        ]

        logger.info("System Supervisor Active - Monitoring Tasks")
        
        try:
            # Block until ANY task fails (Crash) OR all tasks complete (Graceful)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            
            for task in done:
                if task.exception():
                    logger.critical("System Component CRASHED", error=str(task.exception()))
                    # Re-raise to crash container
                    raise task.exception()
                else:
                    logger.info("Task finished gracefully", task=task)

        except asyncio.CancelledError:
            logger.info("Shutdown requested")
        finally:
            self.running = False
            for t in tasks:
                if not t.done():
                    t.cancel()
            
            # Wait for cleanup?
            await asyncio.gather(*tasks, return_exceptions=True)



if __name__ == "__main__":
    system = HFTSystem()
    
    # Graceful Shutdown Handler
    stop_event = asyncio.Event()

    def handle_signal(sig, frame):
        logger.info("Signal received, initiating shutdown...", signal=sig)
        # In asyncio loop?
        # If loop is running, we can schedule cancel.
        # But this handler runs in main thread context.
        pass # The loop catching KeyboardInterrupt or CancelledError handles logic usually.
             # But for docker SIGTERM, we need to bridge to asyncio.
             # Better to stick to asyncio.run handling or add signal handler *inside* run.

    # Actual signal wiring inside system.run() usually better, 
    # but asyncio.run blocks.
    # We can setup signal handlers before run.
    
    try:
        from prometheus_client import start_http_server
        start_http_server(9090)
        logger.info("Metrics server started on :9090")
        
        # We need access to loop to add_signal_handler, but loop created by asyncio.run.
        # Custom run logic:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Signals
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s, loop, system)))

        async def shutdown(sig, loop, system):
            logger.info("Shutdown signal received", signal=sig)
            system.running = False
            # Cancel all tasks? main.run() logic has specific cleanup.
            # We just letting running=False should trigger main loop exit?
            # system.run() checks while self.running: await asyncio.sleep(1)
            # So setting False works.
            
        loop.run_until_complete(system.run())
        
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("System process exit")

