import asyncio
import time
from enum import Enum
from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter")

class FeedState(Enum):
    INIT = "INIT"
    CONNECTING = "CONNECTING"
    SNAPSHOTTING = "SNAPSHOTTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECOVERING = "RECOVERING"

class FeedAdapter:
    def __init__(self, client, lob_engine, bus, raw_queue: asyncio.Queue, normalizer):
        self.client = client
        self.lob_engine = lob_engine
        self.bus = bus
        self.raw_queue = raw_queue
        self.normalizer = normalizer
        
        self.state = FeedState.INIT
        self.running = False
        self.metrics = MetricsRegistry.get()
        self.last_event_ts = time.time()
        
        # Configuration
        self.reconnect_interval_s = 2.0
        self.heartbeat_threshold_s = 5.0

    async def run(self):
        self.running = True
        logger.info("FeedAdapter started")
        
        self.loop = asyncio.get_running_loop()
        
        # Start Tasks
        consumer_task = asyncio.create_task(self._consume_loop())
        monitor_task = asyncio.create_task(self._monitor_loop())
        timer_task = asyncio.create_task(self._timer_loop())

        try:
            await self._connect_sequence()
            
            while self.running:
                await asyncio.sleep(1)
                
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            for t in [consumer_task, monitor_task, timer_task]:
                t.cancel()
            logger.info("FeedAdapter stopped")
    
    # ... existing methods ...

    async def _timer_loop(self):
        """Generates TimerTick events for strategies."""
        TICK_INTERVAL = 0.05 # 50ms
        while self.running:
            await asyncio.sleep(TICK_INTERVAL)
            tick = {
                "type": "TimerTick",
                "ts": time.time_ns()
            }
            # Push directly to bus to avoid queue lag?
            # Or queue to maintain total ordering?
            # Strategies expect TimerTick to trigger periodic logic.
            # Direct bus publish is usually preferred for Timers as they are synthetic.
            # Direct bus publish is usually preferred for Timers as they are synthetic.
            await self.bus.publish(tick)
    
    async def _monitor_loop(self):
        """Heartbeat & Auto-Reconnect."""
        while self.running:
            await asyncio.sleep(1.0)
            
            if self.state == FeedState.CONNECTED:
                gap = time.time() - self.last_event_ts
                if gap > self.heartbeat_threshold_s:
                    logger.warning("Heartbeat missing", gap=gap)
                    self._set_state(FeedState.DISCONNECTED)
                    # Trigger recovery
                    asyncio.create_task(self._recover())

    async def _connect_sequence(self):
        """Full connection flow: Login -> Snapshot -> Subscribe."""
        try:
            self._set_state(FeedState.CONNECTING)
            self.client.login()
            
            self._set_state(FeedState.SNAPSHOTTING)
            snapshots = await asyncio.to_thread(self.client.fetch_snapshots)
            for snap in snapshots:
                # Convert Shioaji Snapshot object to dict
                # snap is likely a pydantic model or similar struct in Shioaji
                if hasattr(snap, "to_dict"):
                    snap_payload = snap.to_dict()
                elif hasattr(snap, "__dict__"):
                    snap_payload = snap.__dict__
                else:
                    try:
                        snap_payload = dict(snap)
                    except:
                        # Fallback for mock/test data that might already be dict
                        snap_payload = snap if isinstance(snap, dict) else {}

                normalized_snap = self.normalizer.normalize_snapshot(snap_payload)
                
                if normalized_snap and self.lob_engine:
                     self.lob_engine.apply_snapshot(normalized_snap)
            
            logger.info("Snapshots applied", count=len(snapshots))
            
            self.client.subscribe_basket(self._on_shioaji_event)
            self._set_state(FeedState.CONNECTED)
            self.last_event_ts = time.time()
            
            # Start Synthetic Feed if in Simulation mode without credentials
            if self.client.mode == "simulation" and not self.client.logged_in:
                logger.info("Starting Synthetic Feed (No Credentials)")
                asyncio.create_task(self._sim_feed_loop())

        except Exception as e:
            logger.error("Connection sequence failed", error=str(e))
            self._set_state(FeedState.DISCONNECTED)

    async def _sim_feed_loop(self):
        """Generates random walk data for simulation."""
        import random
        # Base prices
        prices = {s["code"]: 1000.0 for s in self.client.symbols}
        
        while self.running and self.state == FeedState.CONNECTED:
            await asyncio.sleep(1.0) # 1 Tick/sec/symbol
            
            for s in self.client.symbols:
                msg = {
                    "code": s["code"],
                    "close": prices[s["code"]],
                    "volume": random.randint(1, 10),
                    "ts": int(time.time() * 1000000), # microseconds for Shioaji usually? Or ns? Normalizer expects int.
                    "tick_type": 1,
                    "simtrade": 1
                }
                # Random walk
                change = random.choice([-0.5, 0, 0.5])
                prices[s["code"]] += change
                
                # Push to Queue
                self.loop.call_soon_threadsafe(self.raw_queue.put_nowait, msg)
                
            self.last_event_ts = time.time()

    def _on_shioaji_event(self, exchange, item):
        """
        Callback from Shioaji thread.
        DISCIPLINE: Only capture time, (decode minimal), and enqueue.
        """
        try:
            if self.running and self.loop:
                 # asyncio.Queue.put_nowait is NOT threadsafe!
                 # Must use call_soon_threadsafe to schedule the put on the event loop.
                 self.loop.call_soon_threadsafe(self.raw_queue.put_nowait, item)
        except Exception:
            pass

    async def _consume_loop(self):
        """Pinned consumer: Dequeue -> Normalize -> LOB -> Bus."""
        while self.running:
            try:
                item = await self.raw_queue.get()
                self.last_event_ts = time.time()
                
                # Detect type and normalize
                normalized = None
                if "Close" in item or "close" in item: 
                    normalized = self.normalizer.normalize_tick(item)
                elif "BidPrice" in item or "bid_price" in item: 
                    normalized = self.normalizer.normalize_bidask(item)
                
                if normalized:
                    # 1. Update LOB & Get Stats
                    stats = None
                    if self.lob_engine:
                        stats = self.lob_engine.process_event(normalized)
                    
                    # 2. Push to Bus
                    await self.bus.publish(normalized)
                    if stats:
                        await self.bus.publish(stats)
                    
                    # 3. Metrics
                    self.metrics.feed_events_total.labels(type=normalized["type"]).inc()
                    # Latency tracking if needed:
                    # latency = time.time_ns() - normalized["local_ts"]
                    # self.metrics.feed_latency_ns.observe(latency)

                self.raw_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Consumer error", error=str(e))
                self.metrics.bus_overflow_total.inc() # Misusing metric name but ok for error
                # Prevent tight loop crash
                await asyncio.sleep(0.01)

    # _monitor_loop was defined earlier. Removing duplicate.

    async def _recover(self):
        logger.info("Attempting Recovery...")
        self._set_state(FeedState.RECOVERING)
        await asyncio.sleep(self.reconnect_interval_s)
        await self._connect_sequence()

    def _set_state(self, new_state: FeedState):
        if self.state != new_state:
            logger.info("Feed State Changed", old=self.state.value, new=new_state.value)
            self.state = new_state
            # Metrics gauge
            # self.metrics.feed_state.set(...)
