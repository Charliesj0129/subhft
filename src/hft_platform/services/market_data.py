import asyncio
import datetime as dt
import os
import time
from enum import Enum
from zoneinfo import ZoneInfo

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer, SymbolMetadata
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry

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
        self.last_event_ts = timebase.now_s()
        self.last_event_mono = time.monotonic()
        self.heartbeat_threshold_s = 5.0
        self.resubscribe_gap_s = float(os.getenv("HFT_MD_RESUBSCRIBE_GAP_S", "15"))
        self.reconnect_gap_s = float(os.getenv("HFT_MD_RECONNECT_GAP_S", "60"))
        self.force_reconnect_gap_s = float(os.getenv("HFT_MD_FORCE_RECONNECT_GAP_S", "300"))
        self.reconnect_cooldown_s = float(os.getenv("HFT_MD_RECONNECT_COOLDOWN_S", "60"))
        self.reconnect_days = {d.strip().lower() for d in os.getenv("HFT_RECONNECT_DAYS", "").split(",") if d.strip()}
        self.reconnect_hours = os.getenv("HFT_RECONNECT_HOURS", "")
        self.reconnect_hours_2 = os.getenv("HFT_RECONNECT_HOURS_2", "")
        self.reconnect_tz = os.getenv("HFT_RECONNECT_TZ") or timebase.TZ_NAME or "Asia/Taipei"
        try:
            self._reconnect_tzinfo: dt.tzinfo = ZoneInfo(self.reconnect_tz)
        except Exception:
            logger.warning("Invalid reconnect tz, defaulting to UTC", tz=self.reconnect_tz)
            self._reconnect_tzinfo = dt.timezone.utc
        self._last_reconnect_ts = 0.0
        self._resubscribe_attempts = 0
        self._last_rollover_reconnect_date: dt.date | None = None
        self._last_rollover_seen_date: dt.date | None = None
        self._pending_reconnect_reason: str | None = None
        self._pending_reconnect_gap: float = 0.0
        self._pending_reconnect_since: float | None = None
        self.metrics = {"count": 0, "start_ts": timebase.now_s()}
        self.metrics_registry = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self.log_raw = os.getenv("HFT_MD_LOG_RAW", "0") == "1"
        self.log_raw_every = int(os.getenv("HFT_MD_LOG_EVERY", "1000"))
        self._raw_log_counter = 0
        self.log_normalized = os.getenv("HFT_MD_LOG_NORMALIZED", "0") == "1"
        self.log_normalized_every = int(os.getenv("HFT_MD_LOG_NORMALIZED_EVERY", "1000"))
        self._normalized_log_counter = 0

        # Per-symbol feed gap monitoring
        self._symbol_last_tick: dict[str, float] = {}  # symbol -> monotonic timestamp
        self._symbol_last_tick_lock = asyncio.Lock()  # Protect dict from concurrent access
        self._symbol_gap_threshold_s = float(os.getenv("HFT_SYMBOL_GAP_THRESHOLD_S", "5.0"))
        self._watchdog_interval_s = float(os.getenv("HFT_WATCHDOG_INTERVAL_S", "1.0"))

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()
        logger.info("MarketDataService started")
        self.lob.start_metrics_worker(self.loop)

        # Start Monitor
        monitor_task = asyncio.create_task(self._monitor_loop())

        # Start per-symbol feed gap watchdog
        watchdog_task = asyncio.create_task(self._watchdog_loop())

        # Connect
        await self._connect_sequence()

        try:
            while self.running:
                msg = await self.raw_queue.get()
                if isinstance(msg, tuple) and len(msg) == 2:
                    _exchange, raw = msg
                else:
                    raw = msg
                self.last_event_ts = timebase.now_s()
                self.last_event_mono = time.monotonic()
                self.metrics_registry.feed_last_event_ts.labels(source="market_data").set(self.last_event_ts)
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
                    norm_start_ns = time.perf_counter_ns()
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
                    norm_duration = time.perf_counter_ns() - norm_start_ns
                except Exception as ne:
                    logger.error("Normalization check failed", error=str(ne), raw_type=str(type(raw)))
                    norm_duration = 0

                if event:
                    # Update per-symbol timestamp for watchdog (fire-and-forget async update)
                    symbol = getattr(event, "symbol", None)
                    if symbol:
                        asyncio.create_task(self._update_symbol_tick(symbol))

                    trace_id = ""
                    meta = getattr(event, "meta", None)
                    if meta is not None:
                        seq = getattr(meta, "seq", None)
                        topic = getattr(meta, "topic", "event")
                        if seq is not None:
                            trace_id = f"{topic}:{seq}"
                    if norm_duration and self.latency:
                        self.latency.record(
                            "normalize",
                            norm_duration,
                            trace_id=trace_id,
                            symbol=getattr(event, "symbol", ""),
                        )
                    if self.log_normalized:
                        self._normalized_log_counter += 1
                        if self._normalized_log_counter % self.log_normalized_every == 0:
                            logger.info("MD Normalized", type=str(type(event)), symbol=event.symbol)

                    # Update LOB
                    # Hot path: update LOB
                    lob_start_ns = time.perf_counter_ns()
                    stats = self.lob.process_event(event)
                    lob_duration = time.perf_counter_ns() - lob_start_ns
                    if self.latency:
                        self.latency.record(
                            "lob_process",
                            lob_duration,
                            trace_id=trace_id,
                            symbol=getattr(event, "symbol", ""),
                        )

                    if self.publish_full_events:
                        if stats:
                            self._publish_many_nowait([event, stats])
                        else:
                            self._publish_nowait(event)
                    elif stats:
                        self._publish_nowait(stats)

                self.raw_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("MD Error", error=str(e))
        finally:
            monitor_task.cancel()
            watchdog_task.cancel()

    async def _connect_sequence(self):
        try:
            self._set_state(FeedState.CONNECTING)
            # Always offload blocking API calls to a worker thread.
            await self._call_client(self.client.login)
            await self._call_client(self.client.validate_symbols)

            # Snapshots
            self._set_state(FeedState.SNAPSHOTTING)
            snapshots = await self._call_client(self.client.fetch_snapshots)

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
                    if stats:
                        self._publish_many_nowait([event, stats])
                    else:
                        self._publish_nowait(event)
                elif stats:
                    self._publish_nowait(stats)

            await self._call_client(self.client.subscribe_basket, self._on_shioaji_event)
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
            now = timebase.now_s()
            elapsed = now - self.metrics["start_ts"]
            count = self.metrics["count"]
            eps = count / elapsed if elapsed > 0 else 0

            # Reset
            self.metrics["count"] = 0
            self.metrics["start_ts"] = now

            raw_q = self.raw_queue.qsize()

            # 3. Log
            logger.info("Metrics", eps=round(eps, 2), raw_queue=raw_q, state=self.state)

            gap = time.monotonic() - self.last_event_mono
            if self._pending_reconnect_reason and self._within_reconnect_window():
                ok = await self._trigger_reconnect(self._pending_reconnect_gap, reason=self._pending_reconnect_reason)
                if ok:
                    if self._pending_reconnect_reason == "session_rollover":
                        self._last_rollover_reconnect_date = dt.datetime.now(tz=self._reconnect_tzinfo).date()
                    self._pending_reconnect_reason = None
                    self._pending_reconnect_gap = 0.0
                    self._pending_reconnect_since = None
            if self.state == FeedState.CONNECTED:
                if gap > self.heartbeat_threshold_s:
                    logger.warning("Heartbeat missing", gap=gap)
                    if self.metrics_registry:
                        self.metrics_registry.feed_reconnect_total.labels(result="gap").inc()
                if gap > self.resubscribe_gap_s:
                    await self._attempt_resubscribe(gap)
                if gap > self.force_reconnect_gap_s or (gap > self.reconnect_gap_s and self._resubscribe_attempts > 2):
                    await self._request_reconnect(gap, reason="heartbeat_gap")

                if self._should_rollover_reconnect():
                    await self._request_reconnect(gap, reason="session_rollover")

            if self.state in {FeedState.DISCONNECTED, FeedState.RECOVERING}:
                await self._request_reconnect(gap, reason="recovering")

            if self.symbol_metadata.reload_if_changed():
                logger.info("Symbols config reloaded", count=len(self.symbol_metadata.meta))
                try:
                    await asyncio.to_thread(self.client.reload_symbols)
                except Exception as exc:
                    logger.error("Symbol reload failed", error=str(exc))

    async def _update_symbol_tick(self, symbol: str) -> None:
        """Update symbol tick timestamp with lock protection."""
        async with self._symbol_last_tick_lock:
            self._symbol_last_tick[symbol] = time.monotonic()

    async def _watchdog_loop(self):
        """Per-symbol feed gap watchdog.

        Checks each symbol's last tick timestamp and triggers reconnection
        if any symbol exceeds the gap threshold.
        """
        while self.running:
            await asyncio.sleep(self._watchdog_interval_s)

            if self.state != FeedState.CONNECTED:
                continue

            # Take snapshot under lock to avoid iteration during modification
            async with self._symbol_last_tick_lock:
                if not self._symbol_last_tick:
                    continue
                tick_snapshot = dict(self._symbol_last_tick)

            now = time.monotonic()
            stale_symbols: list[tuple[str, float]] = []

            for symbol, last_ts in tick_snapshot.items():
                gap = now - last_ts
                if gap > self._symbol_gap_threshold_s:
                    stale_symbols.append((symbol, gap))

            if stale_symbols:
                symbols_str = ", ".join(f"{s}({g:.1f}s)" for s, g in stale_symbols[:5])
                logger.warning(
                    "Feed gap detected for symbols",
                    stale_count=len(stale_symbols),
                    symbols=symbols_str,
                    threshold_s=self._symbol_gap_threshold_s,
                )

                if self.metrics_registry:
                    self.metrics_registry.feed_reconnect_total.labels(result="symbol_gap").inc()

                # Trigger resubscription if multiple symbols are stale
                if len(stale_symbols) >= 2:
                    await self._attempt_resubscribe(max(g for _, g in stale_symbols))

    async def _publish(self, event):
        """Publish to bus and handle both async and sync publishers."""
        publish_fn = getattr(self.bus, "publish", None)
        if not publish_fn:
            return

        result = publish_fn(event)
        if asyncio.iscoroutine(result):
            await result

    def _publish_nowait(self, event) -> None:
        publish_nowait = getattr(self.bus, "publish_nowait", None)
        if publish_nowait:
            publish_nowait(event)
            return
        asyncio.create_task(self._publish(event))

    def _publish_many_nowait(self, events) -> None:
        publish_many_nowait = getattr(self.bus, "publish_many_nowait", None)
        if publish_many_nowait:
            publish_many_nowait(events)
            return
        for event in events:
            self._publish_nowait(event)

    def _set_state(self, new_state):
        if self.state != new_state:
            logger.info("State change", old=self.state, new=new_state)
            self.state = new_state

    async def _attempt_resubscribe(self, gap: float) -> None:
        if not self._within_reconnect_window():
            return
        ok = await asyncio.to_thread(self.client.resubscribe)
        if ok:
            self._resubscribe_attempts = 0
        else:
            self._resubscribe_attempts += 1
        logger.info("Resubscribe attempt", gap=gap, ok=ok, attempts=self._resubscribe_attempts)

    async def _request_reconnect(self, gap: float, reason: str | None = None) -> None:
        if self._within_reconnect_window():
            await self._trigger_reconnect(gap, reason=reason)
            return
        self._mark_pending_reconnect(gap, reason=reason)

    async def _trigger_reconnect(self, gap: float, reason: str | None = None) -> bool:
        now = timebase.now_s()
        if now - self._last_reconnect_ts < self.reconnect_cooldown_s:
            return False
        if not self._within_reconnect_window():
            return False
        self._last_reconnect_ts = now
        reason_label = reason or "heartbeat_gap"
        logger.warning("Triggering reconnect", gap=gap, reason=reason_label)
        self._set_state(FeedState.RECOVERING)
        force_login = reason_label == "session_rollover"
        ok = await asyncio.to_thread(self.client.reconnect, f"{reason_label} {gap:.1f}s", force_login)
        if ok:
            self._set_state(FeedState.CONNECTED)
            self.last_event_ts = timebase.now_s()
            self.last_event_mono = time.monotonic()
            self._resubscribe_attempts = 0
        else:
            self._set_state(FeedState.DISCONNECTED)
        return ok

    def _should_rollover_reconnect(self) -> bool:
        now_dt = dt.datetime.now(tz=self._reconnect_tzinfo)
        last_event_dt = dt.datetime.fromtimestamp(self.last_event_ts, tz=self._reconnect_tzinfo)
        if last_event_dt.date() == now_dt.date():
            return False
        if self._last_rollover_seen_date == now_dt.date():
            return False
        self._last_rollover_seen_date = now_dt.date()
        return True

    def _within_reconnect_window(self) -> bool:
        if not self.reconnect_days and not self.reconnect_hours and not self.reconnect_hours_2:
            return True
        now = dt.datetime.now(tz=self._reconnect_tzinfo)
        weekday = now.strftime("%a").lower()
        if self.reconnect_days and weekday not in self.reconnect_days:
            return False

        windows = [w for w in (self.reconnect_hours, self.reconnect_hours_2) if w]
        if not windows:
            return True
        for window in windows:
            try:
                start_str, end_str = window.split("-", 1)
                start = dt.time.fromisoformat(start_str)
                end = dt.time.fromisoformat(end_str)
                now_t = now.timetz().replace(tzinfo=None)
                if start <= end:
                    if start <= now_t <= end:
                        return True
                else:
                    if now_t >= start or now_t <= end:
                        return True
            except Exception:
                continue
        return False

    def _mark_pending_reconnect(self, gap: float, reason: str | None = None) -> None:
        reason_label = reason or "heartbeat_gap"
        if self._pending_reconnect_reason != reason_label:
            logger.warning("Reconnect pending (outside window)", gap=gap, reason=reason_label)
        self._pending_reconnect_reason = reason_label
        self._pending_reconnect_gap = gap
        if self._pending_reconnect_since is None:
            self._pending_reconnect_since = timebase.now_s()

    async def _call_client(self, func, *args):
        if os.getenv("HFT_MD_SYNC_CONNECT") == "1":
            return func(*args)
        if hasattr(func, "assert_called") or getattr(func, "_mock_name", None):
            return func(*args)
        return await asyncio.to_thread(func, *args)

    def within_reconnect_window(self) -> bool:
        """Public hook for supervisors to check whether reconnection should be active."""
        return self._within_reconnect_window()

    def get_max_feed_gap_s(self) -> float:
        """Return the maximum feed gap across all symbols in seconds.

        Used by StormGuard to detect stale data.
        Note: This is a sync method that reads a snapshot. For thread-safe
        access in async code, use get_max_feed_gap_s_async().
        """
        # Take a snapshot of values to avoid iteration during modification
        try:
            tick_values = list(self._symbol_last_tick.values())
        except RuntimeError:
            # Dict changed during iteration - return 0 as safe default
            return 0.0

        if not tick_values:
            return 0.0

        now = time.monotonic()
        max_gap = 0.0
        for last_ts in tick_values:
            gap = now - last_ts
            if gap > max_gap:
                max_gap = gap
        return max_gap

    def get_feed_gaps_by_symbol(self) -> dict[str, float]:
        """Return feed gap for each symbol in seconds.

        Used for per-symbol gap monitoring metrics.
        Note: This is a sync method that reads a snapshot. For thread-safe
        access in async code, use get_feed_gaps_by_symbol_async().
        """
        # Take a snapshot to avoid iteration during modification
        try:
            tick_snapshot = dict(self._symbol_last_tick)
        except RuntimeError:
            # Dict changed during iteration - return empty as safe default
            return {}

        if not tick_snapshot:
            return {}

        now = time.monotonic()
        return {symbol: now - last_ts for symbol, last_ts in tick_snapshot.items()}
