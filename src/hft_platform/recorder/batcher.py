import asyncio
import time
from typing import List, Dict, Any
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
        # Normalize to dict
        if hasattr(row, "__dict__"):
            row = row.__dict__
        elif hasattr(row, "to_dict"):
             row = row.to_dict()
        # msgspec or other structs could be handled here if needed
        # Fallback if it is not a dict?
        if not isinstance(row, dict):
             # Try simple conversion or log warning
             try:
                 row = dict(row)
             except (ValueError, TypeError):
                 # logger.warning("Invalid row type", type=type(row)) 
                 # We might want to skip or wrap it? 
                 # For now assuming it is convertible if not already dict
                 pass

        async with self.lock:
            self.buffer.append(row)
            
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

    async def _flush_locked(self):
        if not self.buffer:
            return
            
        data = self.buffer[:] # Copy
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
