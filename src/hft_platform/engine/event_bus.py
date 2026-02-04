import asyncio
import os
from typing import Any, List

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

# from collections import deque

logger = get_logger("event_bus")

_RUST_ENABLED = os.getenv("HFT_RUST_ACCEL", "1").lower() not in {"0", "false", "no", "off"}
_USE_RUST_BUS = os.getenv("HFT_BUS_RUST", "1").lower() not in {"0", "false", "no", "off"}
_WAIT_MODE = os.getenv("HFT_BUS_WAIT_MODE", "event").lower()

try:
    try:
        from hft_platform import rust_core as _rust_core  # type: ignore[attr-defined]
    except Exception:
        import rust_core as _rust_core

    _RUST_RING = getattr(_rust_core, "FastRingBuffer", None)
except Exception:
    _RUST_RING = None


class RingBufferBus:
    """
    Improved RingBufferBus.
    Uses a single shared buffer (list) and cursors for consumers.
    This mimics the Disruptor pattern:
    - Single Writer (publish) -> writes to buffer[seq % size]
    - Multiple Readers -> track their own local_seq
    """

    def __init__(self, size: int = 65536):
        self.size = size
        self._use_rust = _RUST_ENABLED and _USE_RUST_BUS and _RUST_RING is not None
        self._ring = _RUST_RING(size) if self._use_rust else None
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

    def _publish_unlocked(self, event: Any) -> None:
        next_seq = self.cursor + 1
        if self._use_rust and self._ring is not None:
            self._ring.set(next_seq, event)
        else:
            self.buffer[next_seq % self.size] = event
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
                logger.warning("Consumer lagged too much, skipping", lag=current_cursor - local_seq)
                local_seq = current_cursor - self.size

            while local_seq < current_cursor:
                local_seq += 1
                if self._use_rust and self._ring is not None:
                    event = self._ring.get(local_seq)
                else:
                    event = self.buffer[local_seq % self.size]
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
                logger.warning("Consumer lagged too much, skipping", lag=current_cursor - local_seq)
                local_seq = current_cursor - self.size

            batch: List[Any] = []
            while local_seq < current_cursor and len(batch) < batch_size:
                local_seq += 1
                if self._use_rust and self._ring is not None:
                    event = self._ring.get(local_seq)
                else:
                    event = self.buffer[local_seq % self.size]
                if event is not None:
                    batch.append(event)

            if batch:
                yield batch
