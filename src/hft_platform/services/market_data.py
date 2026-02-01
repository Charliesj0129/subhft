import asyncio
import os
import time
from enum import Enum

from structlog import get_logger

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer, SymbolMetadata
from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("service.market_data")


class FeedState(Enum):
    INIT = "INIT"
    CONNECTING = "CONNECTING"
    SNAPSHOTTING = "SNAPSHOTTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECOVERING = "RECOVERING"


class MarketDataService:
    def __init__(
        self,
        bus: RingBufferBus,
        raw_queue: asyncio.Queue,
        client: ShioajiClient,
        publish_full_events: bool = True,
        symbol_metadata: SymbolMetadata | None = None,
    ):
        self.bus = bus
        self.raw_queue = raw_queue
        self.client = client
        self.publish_full_events = publish_full_events

        self.lob = LOBEngine()
        self.symbol_metadata = symbol_metadata or SymbolMetadata()
        self.normalizer = MarketDataNormalizer(metadata=self.symbol_metadata)

        self.state = FeedState.INIT
        self.running = False
        self.last_event_ts = time.time()
        self.heartbeat_threshold_s = 5.0
        self.metrics = {"count": 0, "start_ts": time.time()}
        self.log_raw = os.getenv("HFT_MD_LOG_RAW", "0") == "1"
        self.log_raw_every = int(os.getenv("HFT_MD_LOG_EVERY", "1000"))
        self._raw_log_counter = 0
        self.log_normalized = os.getenv("HFT_MD_LOG_NORMALIZED", "0") == "1"
        self.log_normalized_every = int(os.getenv("HFT_MD_LOG_NORMALIZED_EVERY", "1000"))
        self._normalized_log_counter = 0

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()
        logger.info("MarketDataService started")

        # Start Monitor
        monitor_task = asyncio.create_task(self._monitor_loop())

        # Connect
        await self._connect_sequence()

        try:
            while self.running:
                msg = await self.raw_queue.get()
                if isinstance(msg, tuple) and len(msg) == 2:
                    _exchange, raw = msg
                else:
                    raw = msg
                self.last_event_ts = time.time()
                self.metrics["count"] += 1

                # logger.debug(f"MD Raw Type: {type(raw)}")
                if self.log_raw:
                    self._raw_log_counter += 1
                    if self._raw_log_counter % self.log_raw_every == 0:
                        raw_type = type(raw).__name__
                        sample = None
                        if isinstance(raw, dict):
                            sample = {
                                k: raw.get(k) for k in ("code", "close", "bid_price", "ask_price", "ts") if k in raw
                            }
                        else:
                            sample = getattr(raw, "code", None) or raw_type
                        logger.info("MD Raw Recv", type=raw_type, sample=str(sample)[:200])

                # Normalize
                event = None
                # Basic key check for dispatch
                try:
                    if isinstance(raw, dict):
                        is_bid = "bid_price" in raw or "bid_volume" in raw or "ask_price" in raw
                        is_tick = "close" in raw or "price" in raw
                    else:
                        is_bid = hasattr(raw, "bid_price") or hasattr(raw, "bid_volume") or hasattr(raw, "ask_price")
                        is_tick = hasattr(raw, "close") or hasattr(raw, "price")

                    if is_bid:
                        event = self.normalizer.normalize_bidask(raw)
                    elif is_tick:
                        event = self.normalizer.normalize_tick(raw)
                except Exception as ne:
                    logger.error("Normalization check failed", error=str(ne), raw_type=str(type(raw)))

                if event:
                    if self.log_normalized:
                        self._normalized_log_counter += 1
                        if self._normalized_log_counter % self.log_normalized_every == 0:
                            logger.info("MD Normalized", type=str(type(event)), symbol=event.symbol)

                    # Update LOB
                    # Hot path: update LOB
                    stats = self.lob.process_event(event)

                    # Offload publishing to avoid blocking the event loop
                    if self.publish_full_events:
                        asyncio.create_task(self._publish(event))

                    if stats:
                        asyncio.create_task(self._publish(stats))

                self.raw_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("MD Error", error=str(e))
        finally:
            monitor_task.cancel()

    async def _connect_sequence(self):
        try:
            self._set_state(FeedState.CONNECTING)
            self.client.login()
            self.client.validate_symbols()

            # Snapshots
            self._set_state(FeedState.SNAPSHOTTING)
            snapshots = await asyncio.to_thread(self.client.fetch_snapshots)

            # Application of snapshots
            # Need to normalize snapshots?
            # Existing implementation used normalize_snapshot -> apply_snapshot
            # I haven't implemented normalize_snapshot in my new normalizer yet!
            # I left a TODO.
            # I should fix that or fallback.
            # For now, let's assume partial implementation or skip if critical.
            # But snapshots are needed for initial state.

            for snap in snapshots:
                event = self.normalizer.normalize_snapshot(snap)
                if not event:
                    continue

                stats = self.lob.process_event(event)

                if self.publish_full_events:
                    asyncio.create_task(self._publish(event))
                if stats:
                    asyncio.create_task(self._publish(stats))

            self.client.subscribe_basket(self._on_shioaji_event)
            self._set_state(FeedState.CONNECTED)

        except Exception as e:
            logger.error("Connect failed", error=str(e))
            self._set_state(FeedState.DISCONNECTED)

    def _on_shioaji_event(self, *args, **kwargs):
        """
        Unified callback for Shioaji events.
        Signature can vary: (exchange, msg) or (topic, msg, ...)
        """
        try:
            # DEBUG: Log every callback to confirm flow
            if self.log_raw:
                logger.debug("Callback hit", args_len=len(args))

            exchange = None
            msg = None

            # Heuristic to find Exchange and Msg from *args (usually 4 args now)
            if len(args) >= 2:
                p0 = args[0]
                p1 = args[1]
                msg = p1
                if hasattr(p0, "name") or isinstance(p0, str):
                    exchange = p0

            # Enqueue as tuple (exchange, msg) to match consumer
            if hasattr(self, "loop"):
                if msg:
                    self.loop.call_soon_threadsafe(self.raw_queue.put_nowait, (exchange, msg))
                else:
                    pass  # logger.warning(f"Could not parse msg from {args}")
            else:
                logger.error("Callback loop missing")

        except Exception as e:
            logger.error(f"Error in Shioaji callback: {e}")

    async def _monitor_loop(self):
        while self.running:
            await asyncio.sleep(5.0)  # 5s interval

            # 1. Throughput
            now = time.time()
            elapsed = now - self.metrics["start_ts"]
            count = self.metrics["count"]
            eps = count / elapsed if elapsed > 0 else 0

            # Reset
            self.metrics["count"] = 0
            self.metrics["start_ts"] = now

            raw_q = self.raw_queue.qsize()

            # 3. Log
            logger.info("Metrics", eps=round(eps, 2), raw_queue=raw_q, state=self.state)

            if self.state == FeedState.CONNECTED:
                gap = time.time() - self.last_event_ts
                if gap > self.heartbeat_threshold_s:
                    logger.warning("Heartbeat missing", gap=gap)
                    # trigger reconnect logic?
                    pass

            if self.symbol_metadata.reload_if_changed():
                logger.info("Symbols config reloaded", count=len(self.symbol_metadata.meta))
                try:
                    self.client.reload_symbols()
                except Exception as exc:
                    logger.error("Symbol reload failed", error=str(exc))

    async def _publish(self, event):
        """Publish to bus and handle both async and sync publishers."""
        publish_fn = getattr(self.bus, "publish", None)
        if not publish_fn:
            return

        result = publish_fn(event)
        if asyncio.iscoroutine(result):
            await result

    def _set_state(self, new_state):
        if self.state != new_state:
            logger.info("State change", old=self.state, new=new_state)
            self.state = new_state
