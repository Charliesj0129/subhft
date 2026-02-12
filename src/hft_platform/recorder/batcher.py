import asyncio
import os
from typing import Any, Dict, List

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.utils.serialization import serialize

logger = get_logger("recorder.batcher")


class BackpressurePolicy:
    """Backpressure handling when buffer is full."""

    DROP_OLDEST = "drop_oldest"  # Drop oldest entries to make room
    DROP_NEWEST = "drop_newest"  # Reject new entries
    BLOCK = "block"  # Block until space available (not recommended for HFT)


class Batcher:
    """
    Accumulates rows per table. Flushes when size > Limit OR time > interval.
    Implements backpressure handling to prevent unbounded memory growth.
    """

    # Maximum buffer size before backpressure kicks in
    DEFAULT_MAX_BUFFER_SIZE = 10000

    def __init__(
        self,
        table_name: str,
        flush_limit: int = 1000,
        flush_interval_ms: int = 500,
        writer=None,
        max_buffer_size: int | None = None,
        backpressure_policy: str = BackpressurePolicy.DROP_NEWEST,
    ):
        self.table_name = table_name
        self.flush_limit = flush_limit
        self.flush_interval_ms = flush_interval_ms
        self.writer = writer

        # Backpressure configuration
        self.max_buffer_size = max_buffer_size or int(
            os.getenv("HFT_BATCHER_MAX_BUFFER", str(self.DEFAULT_MAX_BUFFER_SIZE))
        )
        self.backpressure_policy = backpressure_policy

        self.buffer: List[Dict[str, Any]] = []
        self.last_flush_time = timebase.now_s()
        self.lock = asyncio.Lock()

        # Metrics for monitoring backpressure events
        self.dropped_count = 0
        self.total_count = 0

    def _serialize_row(self, row: Any) -> Dict[str, Any] | None:
        """Serialize a single row to dict, return None on failure."""
        row_dict = serialize(row)
        if isinstance(row_dict, dict):
            return row_dict
        try:
            return dict(row_dict)
        except Exception as e:
            logger.warning(
                "Failed to convert row to dict, skipping",
                table=self.table_name,
                row_type=type(row).__name__,
                error=str(e),
            )
            return None

    async def add(self, row: Any):
        row_dict = self._serialize_row(row)
        if row_dict is None:
            return

        async with self.lock:
            self.total_count += 1
            self._apply_backpressure_locked(1)

            if len(self.buffer) < self.max_buffer_size:
                self.buffer.append(row_dict)

            if len(self.buffer) >= self.flush_limit:
                await self._flush_locked()

    async def add_many(self, rows: list[Any]):
        """Add multiple rows under a single lock acquisition (CC-5)."""
        dicts = []
        for row in rows:
            d = self._serialize_row(row)
            if d is not None:
                dicts.append(d)
        if not dicts:
            return

        async with self.lock:
            self.total_count += len(dicts)
            available = self.max_buffer_size - len(self.buffer)
            if available < len(dicts):
                self._apply_backpressure_locked(len(dicts) - available)
                dicts = dicts[:available] if available > 0 else []

            if dicts:
                self.buffer.extend(dicts)

            if len(self.buffer) >= self.flush_limit:
                await self._flush_locked()

    def _apply_backpressure_locked(self, overflow_count: int) -> None:
        """Handle backpressure when buffer is at capacity."""
        if len(self.buffer) < self.max_buffer_size:
            return
        if self.backpressure_policy == BackpressurePolicy.DROP_OLDEST:
            drop_count = max(overflow_count, len(self.buffer) - self.max_buffer_size + 1)
            self.buffer = self.buffer[drop_count:]
            self.dropped_count += drop_count
            if self.dropped_count % 1000 == 0:
                logger.warning(
                    "Backpressure: dropped oldest rows",
                    table=self.table_name,
                    dropped=self.dropped_count,
                    total=self.total_count,
                )
        elif self.backpressure_policy == BackpressurePolicy.DROP_NEWEST:
            self.dropped_count += overflow_count
            if self.dropped_count % 1000 == 0:
                logger.warning(
                    "Backpressure: rejecting new rows",
                    table=self.table_name,
                    dropped=self.dropped_count,
                    total=self.total_count,
                )

    async def check_flush(self):
        """Called periodically by worker."""
        async with self.lock:
            if not self.buffer:
                return

            age = (timebase.now_s() - self.last_flush_time) * 1000
            if age >= self.flush_interval_ms:
                await self._flush_locked()

    async def force_flush(self):
        """Manually trigger flush (e.g. shutdown)."""
        async with self.lock:
            await self._flush_locked()

    async def _flush_locked(self):
        if not self.buffer:
            return

        data = self.buffer[:]  # Copy
        self.buffer.clear()
        self.last_flush_time = timebase.now_s()

        if self.writer:
            try:
                await self.writer.write(self.table_name, data)
            except asyncio.TimeoutError:
                logger.error(
                    "Write timeout - data written to WAL",
                    table=self.table_name,
                    count=len(data),
                )
                # Writer.write already has WAL fallback
            except ConnectionError as e:
                logger.error(
                    "Connection error during write",
                    table=self.table_name,
                    error=str(e),
                    count=len(data),
                )
            except Exception as e:
                logger.error(
                    "Write failed",
                    table=self.table_name,
                    error=str(e),
                    error_type=type(e).__name__,
                    count=len(data),
                )
