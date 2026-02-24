import asyncio
import importlib
import os
from typing import Any, Callable, List, Optional

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

# from collections import deque

logger = get_logger("event_bus")

_RUST_ENABLED = os.getenv("HFT_RUST_ACCEL", "1").lower() not in {"0", "false", "no", "off"}
_USE_RUST_BUS = os.getenv("HFT_BUS_RUST", "1").lower() not in {"0", "false", "no", "off"}
_WAIT_MODE = os.getenv("HFT_BUS_WAIT_MODE", "event").lower()
_USE_TYPED_TICK_RING = os.getenv("HFT_BUS_TYPED_TICK_RING", "0").lower() not in {"0", "false", "no", "off"}
_USE_TYPED_BOOK_RINGS = os.getenv("HFT_BUS_TYPED_BOOK_RINGS", "0").lower() not in {"0", "false", "no", "off"}
_TYPED_BIDASK_PACKED_LEVELS = max(1, int(os.getenv("HFT_BUS_TYPED_BIDASK_PACKED_LEVELS", "5")))

try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except Exception:
        _rust_core = importlib.import_module("rust_core")

    _RUST_RING_FACTORY: Optional[Callable[[int], Any]] = getattr(_rust_core, "FastRingBuffer", None)
    _RUST_TICK_RING_FACTORY: Optional[Callable[[int], Any]] = getattr(_rust_core, "FastTickRingBuffer", None)
    _RUST_BIDASK_RING_FACTORY: Optional[Callable[[int], Any]] = getattr(_rust_core, "FastBidAskRingBuffer", None)
    _RUST_LOBSTATS_RING_FACTORY: Optional[Callable[[int], Any]] = getattr(_rust_core, "FastLOBStatsRingBuffer", None)
except Exception:
    _RUST_RING_FACTORY = None
    _RUST_TICK_RING_FACTORY = None
    _RUST_BIDASK_RING_FACTORY = None
    _RUST_LOBSTATS_RING_FACTORY = None


class _PyFastTickRingBuffer:
    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self.buffer: list[tuple[str, int, int, int, bool, bool, int] | None] = [None] * self.size

    def set_tick(
        self,
        idx: int,
        symbol: str,
        price: int,
        volume: int,
        total_volume: int,
        is_simtrade: bool,
        is_odd_lot: bool,
        exch_ts: int,
    ) -> None:
        self.buffer[int(idx) % self.size] = (
            str(symbol),
            int(price),
            int(volume),
            int(total_volume),
            bool(is_simtrade),
            bool(is_odd_lot),
            int(exch_ts),
        )

    def get(self, idx: int) -> Any | None:
        frame = self.buffer[int(idx) % self.size]
        if frame is None:
            return None
        return ("tick",) + frame


class _PyFastBidAskRingBuffer:
    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self.buffer: list[tuple[Any, ...] | None] = [None] * self.size

    def set_bidask(
        self,
        idx: int,
        symbol: str,
        bids: Any,
        asks: Any,
        exch_ts: int,
        is_snapshot: bool,
        has_stats: bool,
        best_bid: int,
        best_ask: int,
        bid_depth: int,
        ask_depth: int,
        mid_price: float,
        spread: float,
        imbalance: float,
    ) -> None:
        if has_stats:
            self.buffer[int(idx) % self.size] = (
                "bidask",
                str(symbol),
                bids,
                asks,
                int(exch_ts),
                bool(is_snapshot),
                int(best_bid),
                int(best_ask),
                int(bid_depth),
                int(ask_depth),
                float(mid_price),
                float(spread),
                float(imbalance),
            )
        else:
            self.buffer[int(idx) % self.size] = (
                "bidask",
                str(symbol),
                bids,
                asks,
                int(exch_ts),
                bool(is_snapshot),
            )

    def set_bidask_packed(
        self,
        idx: int,
        symbol: str,
        bid_flat: Any,
        bid_rows: int,
        ask_flat: Any,
        ask_rows: int,
        exch_ts: int,
        is_snapshot: bool,
        has_stats: bool,
        best_bid: int,
        best_ask: int,
        bid_depth: int,
        ask_depth: int,
        mid_price: float,
        spread: float,
        imbalance: float,
    ) -> None:
        self.buffer[int(idx) % self.size] = (
            "bidask_packed",
            str(symbol),
            tuple(int(x) for x in bid_flat),
            int(bid_rows),
            tuple(int(x) for x in ask_flat),
            int(ask_rows),
            int(exch_ts),
            bool(is_snapshot),
            bool(has_stats),
            int(best_bid),
            int(best_ask),
            int(bid_depth),
            int(ask_depth),
            float(mid_price),
            float(spread),
            float(imbalance),
        )

    def get(self, idx: int) -> Any | None:
        frame = self.buffer[int(idx) % self.size]
        if not frame:
            return frame
        if frame[0] != "bidask_packed":
            return frame
        (
            _,
            symbol,
            bid_flat,
            bid_rows,
            ask_flat,
            ask_rows,
            exch_ts,
            is_snapshot,
            has_stats,
            best_bid,
            best_ask,
            bid_depth,
            ask_depth,
            mid_price,
            spread,
            imbalance,
        ) = frame
        bids = [[int(bid_flat[i * 2]), int(bid_flat[i * 2 + 1])] for i in range(int(bid_rows))]
        asks = [[int(ask_flat[i * 2]), int(ask_flat[i * 2 + 1])] for i in range(int(ask_rows))]
        if has_stats:
            return (
                "bidask",
                symbol,
                bids,
                asks,
                exch_ts,
                is_snapshot,
                best_bid,
                best_ask,
                bid_depth,
                ask_depth,
                mid_price,
                spread,
                imbalance,
            )
        return ("bidask", symbol, bids, asks, exch_ts, is_snapshot)


def _pack_book_levels(levels: Any, max_levels: int = _TYPED_BIDASK_PACKED_LEVELS) -> tuple[tuple[int, ...], int] | None:
    if levels is None:
        return (), 0
    if isinstance(levels, (str, bytes)):
        return None
    try:
        flat: list[int] = []
        rows = 0
        for row in levels:
            if rows >= max_levels:
                break
            if row is None:
                continue
            try:
                price = int(row[0])
                vol = int(row[1])
            except Exception:
                return None
            flat.extend((price, vol))
            rows += 1
        return tuple(flat), rows
    except Exception:
        return None


class _PyFastLOBStatsRingBuffer:
    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self.buffer: list[tuple[str, int, int, int, float, int, int, int, int] | None] = [None] * self.size

    def set_stats(
        self,
        idx: int,
        symbol: str,
        ts: int,
        mid_price_x2: int,
        spread_scaled: int,
        imbalance: float,
        best_bid: int,
        best_ask: int,
        bid_depth: int,
        ask_depth: int,
    ) -> None:
        self.buffer[int(idx) % self.size] = (
            str(symbol),
            int(ts),
            int(mid_price_x2),
            int(spread_scaled),
            float(imbalance),
            int(best_bid),
            int(best_ask),
            int(bid_depth),
            int(ask_depth),
        )

    def get(self, idx: int) -> Any | None:
        return self.buffer[int(idx) % self.size]


class RingBufferBus:
    """
    Improved RingBufferBus.
    Uses a single shared buffer (list) and cursors for consumers.
    This mimics the Disruptor pattern:
    - Single Writer (publish) -> writes to buffer[seq % size]
    - Multiple Readers -> track their own local_seq
    """

    def __init__(self, size: int = 65536, storm_guard: Any = None):
        self.size = size
        self._use_rust = _RUST_ENABLED and _USE_RUST_BUS and _RUST_RING_FACTORY is not None
        ring_factory = _RUST_RING_FACTORY
        self._ring = ring_factory(size) if self._use_rust and ring_factory is not None else None
        self._use_typed_tick_ring = bool(_USE_TYPED_TICK_RING)
        self._use_typed_book_rings = bool(_USE_TYPED_BOOK_RINGS)
        tick_factory = _RUST_TICK_RING_FACTORY or _PyFastTickRingBuffer
        self._tick_ring = tick_factory(size) if self._use_typed_tick_ring else None
        bidask_factory = _RUST_BIDASK_RING_FACTORY or _PyFastBidAskRingBuffer
        self._bidask_ring = bidask_factory(size) if self._use_typed_book_rings else None
        lobstats_factory = _RUST_LOBSTATS_RING_FACTORY or _PyFastLOBStatsRingBuffer
        self._lobstats_ring = lobstats_factory(size) if self._use_typed_book_rings else None
        self._kind_ring: list[int] | None = (
            [0] * size if (self._use_typed_tick_ring or self._use_typed_book_rings) else None
        )
        self.buffer: List[Any] | None = None if self._use_rust else [None] * size
        self.cursor: int = -1  # Writing cursor
        self.single_writer = os.getenv("HFT_BUS_SINGLE_WRITER", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self.write_lock = asyncio.Lock()
        self.metrics = MetricsRegistry.get()
        # Event for lock-free notification (optional)
        self.signal = None if _WAIT_MODE == "spin" else asyncio.Event()
        self._notify_every = max(1, int(os.getenv("HFT_BUS_NOTIFY_EVERY", "1")))
        self._notify_counter = 0
        self._spin_sleep = float(os.getenv("HFT_BUS_SPIN_SLEEP", "0"))
        self._spin_budget = max(1, int(os.getenv("HFT_BUS_SPIN_BUDGET", "100")))
        # StormGuard reference for triggering HALT on critical overflow
        self._storm_guard = storm_guard
        # Overflow threshold for triggering HALT (consecutive overflows)
        self._overflow_halt_threshold = int(os.getenv("HFT_BUS_OVERFLOW_HALT_THRESHOLD", "3"))
        self._overflow_count = 0

    def set_storm_guard(self, storm_guard: Any) -> None:
        """Set StormGuard reference for overflow HALT triggering."""
        self._storm_guard = storm_guard

    def _publish_unlocked(self, event: Any) -> None:
        next_seq = self.cursor + 1
        handled_by_typed_ring = False
        if (
            self._use_typed_tick_ring
            and self._tick_ring is not None
            and isinstance(event, tuple)
            and len(event) >= 8
            and event[0] == "tick"
        ):
            try:
                _, symbol, price, volume, total_volume, is_simtrade, is_odd_lot, exch_ts = event[:8]
                self._tick_ring.set_tick(
                    next_seq,
                    str(symbol),
                    int(price),
                    int(volume),
                    int(total_volume),
                    bool(is_simtrade),
                    bool(is_odd_lot),
                    int(exch_ts),
                )
                if self._kind_ring is not None:
                    self._kind_ring[next_seq % self.size] = 1
                handled_by_typed_ring = True
            except Exception:
                handled_by_typed_ring = False
        elif (
            self._use_typed_book_rings
            and self._bidask_ring is not None
            and isinstance(event, tuple)
            and len(event) >= 6
            and event[0] == "bidask"
        ):
            try:
                _, symbol, bids, asks, exch_ts, is_snapshot, *rest = event
                if len(rest) >= 7:
                    best_bid, best_ask, bid_depth, ask_depth, mid_price, spread, imbalance = rest[:7]
                    has_stats = True
                else:
                    best_bid = best_ask = bid_depth = ask_depth = 0
                    mid_price = spread = imbalance = 0.0
                    has_stats = False
                packed_writer = getattr(self._bidask_ring, "set_bidask_packed", None)
                used_packed = False
                if callable(packed_writer):
                    packed_bids = _pack_book_levels(bids)
                    packed_asks = _pack_book_levels(asks)
                    if packed_bids is not None and packed_asks is not None:
                        bid_flat, bid_rows = packed_bids
                        ask_flat, ask_rows = packed_asks
                        packed_writer(
                            next_seq,
                            str(symbol),
                            bid_flat,
                            int(bid_rows),
                            ask_flat,
                            int(ask_rows),
                            int(exch_ts),
                            bool(is_snapshot),
                            bool(has_stats),
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                        used_packed = True
                if not used_packed:
                    self._bidask_ring.set_bidask(
                        next_seq,
                        str(symbol),
                        bids,
                        asks,
                        int(exch_ts),
                        bool(is_snapshot),
                        bool(has_stats),
                        int(best_bid),
                        int(best_ask),
                        int(bid_depth),
                        int(ask_depth),
                        float(mid_price),
                        float(spread),
                        float(imbalance),
                    )
                if self._kind_ring is not None:
                    self._kind_ring[next_seq % self.size] = 2
                handled_by_typed_ring = True
            except Exception:
                handled_by_typed_ring = False
        elif (
            self._use_typed_book_rings
            and self._lobstats_ring is not None
            and isinstance(event, tuple)
            and len(event) == 9
            and isinstance(event[0], str)
        ):
            try:
                symbol, ts, mid_x2, spread, imbalance, best_bid, best_ask, bid_depth, ask_depth = event
                self._lobstats_ring.set_stats(
                    next_seq,
                    symbol,
                    int(ts),
                    int(mid_x2),
                    int(spread),
                    float(imbalance),
                    int(best_bid),
                    int(best_ask),
                    int(bid_depth),
                    int(ask_depth),
                )
                if self._kind_ring is not None:
                    self._kind_ring[next_seq % self.size] = 3
                handled_by_typed_ring = True
            except Exception:
                handled_by_typed_ring = False

        if not handled_by_typed_ring:
            if self._kind_ring is not None:
                self._kind_ring[next_seq % self.size] = 0
            if self._use_rust and self._ring is not None:
                self._ring.set(next_seq, event)
            else:
                buffer = self.buffer
                if buffer is None:
                    buffer = [None] * self.size
                    self.buffer = buffer
                buffer[next_seq % self.size] = event
        self.cursor = next_seq
        self._notify_counter += 1

    def _notify(self) -> None:
        if self.signal is not None and self._notify_counter % self._notify_every == 0:
            self.signal.set()

    def publish_nowait(self, event: Any) -> None:
        """Synchronous, lock-free publish (single-threaded)."""
        self._publish_unlocked(event)
        self._notify()

    def publish_many_nowait(self, events: List[Any]) -> None:
        """Synchronous, lock-free publish for a batch (single-threaded)."""
        for event in events:
            self._publish_unlocked(event)
        self._notify()

    async def publish(self, event: Any):
        """Publish event to shared buffer."""
        if self.single_writer:
            # Single-writer fast path: no lock
            self._publish_unlocked(event)
            self._notify()
            return

        async with self.write_lock:
            # Check if we are overwriting unread data?
            # For simplicity in this non-blocking design, we overwrite.
            # Ideally we track min_reader_cursor to prevent overwrite if strict usage.
            # But for HFT, latest data > stalled consumer.
            self._publish_unlocked(event)
            if self.signal is not None:
                self.signal.set()

    async def publish_many(self, events: List[Any]):
        """Publish a batch of events."""
        if self.single_writer:
            for event in events:
                self._publish_unlocked(event)
            self._notify()
            return

        async with self.write_lock:
            for event in events:
                self._publish_unlocked(event)
            if self.signal is not None:
                self.signal.set()

    async def consume(self, start_cursor: int | None = None):
        """Async generator for consuming events."""
        # If start_cursor is None, join at current (latest).
        # To replay from beginning, pass -1.
        local_seq = self.cursor if start_cursor is None else start_cursor

        while True:
            # Wait for data
            while self.cursor <= local_seq:
                if self.signal is not None:
                    await self.signal.wait()
                    # Allow other consumers to proceed without contention.
                    self.signal.clear()
                else:
                    # Spin-wait mode: lock-free signaling via cursor polling.
                    if self._spin_sleep <= 0:
                        for _ in range(self._spin_budget):
                            if self.cursor > local_seq:
                                break
                        if self.cursor <= local_seq:
                            await asyncio.sleep(0)
                    else:
                        await asyncio.sleep(self._spin_sleep)

            # Catch up batch
            current_cursor = self.cursor
            # Don't read more than size at once (buffer wrap protection for very slow consumer)
            if current_cursor - local_seq > self.size:
                # Lagged too much, skip to latest - size
                self.metrics.bus_overflow_total.inc()
                self._overflow_count += 1
                lag = current_cursor - local_seq
                logger.error(
                    "CRITICAL: Consumer lagged too much, data loss occurred",
                    lag=lag,
                    overflow_count=self._overflow_count,
                    threshold=self._overflow_halt_threshold,
                )

                # Trigger StormGuard HALT on repeated overflows
                if self._overflow_count >= self._overflow_halt_threshold and self._storm_guard is not None:
                    halt_msg = f"EventBus overflow: {self._overflow_count} overflows, lag={lag}"
                    self._storm_guard.trigger_halt(halt_msg)
                    logger.critical("StormGuard HALT triggered due to EventBus overflow")

                local_seq = current_cursor - self.size

            while local_seq < current_cursor:
                local_seq += 1
                kind = self._kind_ring[local_seq % self.size] if self._kind_ring is not None else 0
                if kind == 1 and self._tick_ring is not None:
                    event = self._tick_ring.get(local_seq)
                elif kind == 2 and self._bidask_ring is not None:
                    event = self._bidask_ring.get(local_seq)
                elif kind == 3 and self._lobstats_ring is not None:
                    event = self._lobstats_ring.get(local_seq)
                elif self._use_rust and self._ring is not None:
                    event = self._ring.get(local_seq)
                else:
                    buffer = self.buffer
                    if buffer is None:
                        buffer = [None] * self.size
                        self.buffer = buffer
                    event = buffer[local_seq % self.size]
                if event is not None:
                    yield event
                # yield to loop to allow other tasks to run if batch is huge
                # if local_seq % 100 == 0: await asyncio.sleep(0)

    async def consume_batch(self, batch_size: int, start_cursor: int | None = None):
        """Async generator yielding lists of events."""
        batch_size = max(1, batch_size)
        local_seq = self.cursor if start_cursor is None else start_cursor

        while True:
            while self.cursor <= local_seq:
                if self.signal is not None:
                    await self.signal.wait()
                    self.signal.clear()
                else:
                    if self._spin_sleep <= 0:
                        for _ in range(self._spin_budget):
                            if self.cursor > local_seq:
                                break
                        if self.cursor <= local_seq:
                            await asyncio.sleep(0)
                    else:
                        await asyncio.sleep(self._spin_sleep)

            current_cursor = self.cursor
            if current_cursor - local_seq > self.size:
                self.metrics.bus_overflow_total.inc()
                self._overflow_count += 1
                lag = current_cursor - local_seq
                logger.error(
                    "CRITICAL: Consumer batch lagged too much, data loss occurred",
                    lag=lag,
                    overflow_count=self._overflow_count,
                    threshold=self._overflow_halt_threshold,
                )

                # Trigger StormGuard HALT on repeated overflows
                if self._overflow_count >= self._overflow_halt_threshold and self._storm_guard is not None:
                    halt_msg = f"EventBus batch overflow: {self._overflow_count} overflows, lag={lag}"
                    self._storm_guard.trigger_halt(halt_msg)
                    logger.critical("StormGuard HALT triggered due to EventBus batch overflow")

                local_seq = current_cursor - self.size

            batch: List[Any] = []
            while local_seq < current_cursor and len(batch) < batch_size:
                local_seq += 1
                kind = self._kind_ring[local_seq % self.size] if self._kind_ring is not None else 0
                if kind == 1 and self._tick_ring is not None:
                    event = self._tick_ring.get(local_seq)
                elif kind == 2 and self._bidask_ring is not None:
                    event = self._bidask_ring.get(local_seq)
                elif kind == 3 and self._lobstats_ring is not None:
                    event = self._lobstats_ring.get(local_seq)
                elif self._use_rust and self._ring is not None:
                    event = self._ring.get(local_seq)
                else:
                    buffer = self.buffer
                    if buffer is None:
                        buffer = [None] * self.size
                        self.buffer = buffer
                    event = buffer[local_seq % self.size]
                if event is not None:
                    batch.append(event)

            if batch:
                yield batch
