import asyncio
from typing import Any, List
from structlog import get_logger
from hft_platform.observability.metrics import MetricsRegistry
# from collections import deque

logger = get_logger("event_bus")

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
        self.buffer: List[Any] = [None] * size
        self.cursor: int = -1 # Writing cursor
        self.write_lock = asyncio.Lock()
        self.metrics = MetricsRegistry.get()
        # Condition variable to notify readers of new data
        self.signal = asyncio.Condition()

    async def publish(self, event: Any):
        """Publish event to shared buffer."""
        async with self.write_lock:
            # Check if we are overwriting unread data? 
            # For simplicity in this non-blocking design, we overwrite.
            # Ideally we track min_reader_cursor to prevent overwrite if strict usage.
            # But for HFT, latest data > stalled consumer.
            
            next_seq = self.cursor + 1
            self.buffer[next_seq % self.size] = event
            self.cursor = next_seq
            
            async with self.signal:
                self.signal.notify_all()

    async def consume(self, start_cursor: int = None):
        """Async generator for consuming events."""
        # If start_cursor is None, join at current (latest).
        # To replay from beginning, pass -1.
        local_seq = self.cursor if start_cursor is None else start_cursor
        
        while True:
            # Wait for data
            async with self.signal:
                await self.signal.wait_for(lambda: self.cursor > local_seq)
            
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
                event = self.buffer[local_seq % self.size]
                if event is not None:
                    yield event
                # yield to loop to allow other tasks to run if batch is huge
                # if local_seq % 100 == 0: await asyncio.sleep(0)
