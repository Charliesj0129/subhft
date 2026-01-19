import asyncio
import time
from typing import Any, Dict, List

from structlog import get_logger

logger = get_logger("recorder.batcher")


class Batcher:
    """
    Accumulates rows per table. Flushes when size > Limit OR time > interval.
    """

    def __init__(self, table_name: str, flush_limit: int = 1000, flush_interval_ms: int = 500, writer=None):
        self.table_name = table_name
        self.flush_limit = flush_limit
        self.flush_interval_ms = flush_interval_ms
        self.writer = writer

        self.buffer: List[Dict[str, Any]] = []
        self.last_flush_time = time.time()
        self.lock = asyncio.Lock()

    async def add(self, row: Any):
        # Normalize to dict using shared utility
        from hft_platform.utils.serialization import serialize

        row_dict = serialize(row)

        # Ensure it is a dict
        if not isinstance(row_dict, dict):
            # Try primitive conversion or wrap?
            # If it's a list (bulk add?), Batcher.add usually takes single row
            # If it's a list, maybe we should extend?
            # For now, valid row is dict.
            # If serialize returned something else (e.g. enum val), it's not a row.
            try:
                row_dict = dict(row_dict)
            except Exception:
                # logger.warning("Invalid row", type=type(row))
                return  # Skip invalid

        async with self.lock:
            self.buffer.append(row_dict)

            if len(self.buffer) >= self.flush_limit:
                await self._flush_locked()

    async def check_flush(self):
        """Called periodically by worker."""
        async with self.lock:
            if not self.buffer:
                return

            age = (time.time() - self.last_flush_time) * 1000
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
        self.last_flush_time = time.time()

        # Async write (fire and forget or wait?)
        # For data safety, we might want to wait or queue to writer
        if self.writer:
            try:
                await self.writer.write(self.table_name, data)
            except Exception as e:
                logger.error("Write failed", table=self.table_name, error=str(e), count=len(data))
                # TODO: WAL fallback here
