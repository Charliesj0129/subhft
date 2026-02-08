"""Dead Letter Queue for rejected/failed orders.

Provides persistent storage for orders that fail to execute, enabling:
- Post-mortem analysis of rejections
- Retry mechanisms for transient failures
- Audit trail for compliance

Complies with HFT Laws:
- Allocator Law: Pre-allocated buffer, bounded queue
- Async Law: Non-blocking writes via executor
"""

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("order.deadletter")


class RejectionReason(str, Enum):
    """Categorized reasons for order rejection."""

    CIRCUIT_BREAKER = "circuit_breaker"
    RATE_LIMIT = "rate_limit"
    API_TIMEOUT = "api_timeout"
    CONNECTION_ERROR = "connection_error"
    VALIDATION_ERROR = "validation_error"
    BROKER_REJECT = "broker_reject"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNKNOWN = "unknown"


@dataclass
class DeadLetterEntry:
    """A rejected order entry in the dead letter queue."""

    timestamp_ns: int
    order_id: str
    strategy_id: str
    symbol: str
    side: str
    price: int  # Scaled price
    qty: int
    reason: str
    error_message: str
    intent_type: str = "NEW"
    metadata: Dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    trace_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeadLetterEntry":
        return cls(**data)


class DeadLetterQueue:
    """
    Persistent dead letter queue for failed orders.

    Features:
    - Bounded in-memory buffer with overflow to disk
    - Async-safe operations
    - JSON-lines file format for easy analysis
    """

    DEFAULT_BUFFER_SIZE = 1000
    DEFAULT_DIR = ".dlq"

    def __init__(
        self,
        dlq_dir: Optional[str] = None,
        max_buffer_size: int = DEFAULT_BUFFER_SIZE,
    ):
        self.dlq_dir = Path(dlq_dir if dlq_dir else os.getenv("HFT_DLQ_DIR", self.DEFAULT_DIR))
        self.dlq_dir.mkdir(parents=True, exist_ok=True)
        self.max_buffer_size = max_buffer_size

        # In-memory buffer (pre-allocated capacity)
        self._buffer: List[DeadLetterEntry] = []
        self._lock = asyncio.Lock()

        # Stats
        self.total_entries = 0
        self.total_flushed = 0

        logger.info("DeadLetterQueue initialized", dir=str(self.dlq_dir), max_buffer=max_buffer_size)

    async def add(
        self,
        order_id: str,
        strategy_id: str,
        symbol: str,
        side: str,
        price: int,
        qty: int,
        reason: RejectionReason | str,
        error_message: str,
        intent_type: str = "NEW",
        metadata: Optional[Dict[str, Any]] = None,
        trace_id: str = "",
    ) -> None:
        """Add a rejected order to the dead letter queue."""
        entry = DeadLetterEntry(
            timestamp_ns=timebase.now_ns(),
            order_id=order_id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            reason=str(reason.value if isinstance(reason, RejectionReason) else reason),
            error_message=error_message,
            intent_type=intent_type,
            metadata=metadata or {},
            trace_id=trace_id,
        )

        async with self._lock:
            self._buffer.append(entry)
            self.total_entries += 1

            # Flush to disk if buffer is full
            if len(self._buffer) >= self.max_buffer_size:
                await self._flush_locked()

        logger.warning(
            "Order added to DLQ",
            order_id=order_id,
            strategy_id=strategy_id,
            symbol=symbol,
            reason=entry.reason,
        )

    async def flush(self) -> int:
        """Manually flush buffer to disk. Returns number of entries flushed."""
        async with self._lock:
            return await self._flush_locked()

    async def _flush_locked(self) -> int:
        """Flush buffer to disk (must hold lock)."""
        if not self._buffer:
            return 0

        entries = self._buffer[:]
        self._buffer.clear()

        # Write to file in executor (non-blocking)
        loop = asyncio.get_running_loop()
        count = await loop.run_in_executor(None, self._write_entries, entries)
        self.total_flushed += count
        return count

    def _write_entries(self, entries: List[DeadLetterEntry]) -> int:
        """Blocking write to disk (run in executor)."""
        ts = int(timebase.now_ns())
        filename = self.dlq_dir / f"dlq_{ts}.jsonl"

        try:
            with open(filename, "w") as f:
                for entry in entries:
                    f.write(json.dumps(entry.to_dict()) + "\n")
            logger.info("DLQ flushed to disk", file=str(filename), count=len(entries))
            return len(entries)
        except Exception as e:
            logger.error("DLQ flush failed", error=str(e))
            return 0

    async def get_stats(self) -> Dict[str, int]:
        """Get queue statistics."""
        async with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "total_entries": self.total_entries,
                "total_flushed": self.total_flushed,
            }

    def read_all(self, limit: int = 1000) -> List[DeadLetterEntry]:
        """Read entries from disk files (for analysis). Synchronous."""
        entries: List[DeadLetterEntry] = []
        files = sorted(self.dlq_dir.glob("dlq_*.jsonl"), reverse=True)

        for fpath in files:
            if len(entries) >= limit:
                break
            try:
                with open(fpath, "r") as f:
                    for line in f:
                        if len(entries) >= limit:
                            break
                        try:
                            data = json.loads(line)
                            entries.append(DeadLetterEntry.from_dict(data))
                        except (json.JSONDecodeError, TypeError):
                            continue
            except Exception:
                continue

        return entries


# Global singleton instance
_dlq_instance: Optional[DeadLetterQueue] = None


def get_dlq() -> DeadLetterQueue:
    """Get or create the global DLQ instance."""
    global _dlq_instance
    if _dlq_instance is None:
        _dlq_instance = DeadLetterQueue()
    return _dlq_instance
