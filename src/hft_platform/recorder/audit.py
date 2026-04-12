"""Audit table writers for orders, risk decisions, and guardrail transitions.

Non-blocking audit logging via bounded asyncio.Queue with drop-on-full semantics.
Background flush tasks batch rows and write to ClickHouse audit tables.
Falls back to structlog if ClickHouse is unavailable.
"""

from __future__ import annotations

import asyncio
import collections
import os
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("recorder.audit")

_DEFAULT_QUEUE_SIZE = 10_000
_DEFAULT_FLUSH_INTERVAL_MS = 1_000
_DEFAULT_FLUSH_LIMIT = 500
_DEFAULT_OVERFLOW_SIZE = 50_000

# Singleton instance
_audit_writer: AuditWriter | None = None


class AuditWriter:
    """Non-blocking audit writer with bounded queues and background flush.

    Three independent queues feed three ClickHouse audit tables:
      - audit.orders_log
      - audit.risk_log
      - audit.guardrail_log

    All public methods use put_nowait with QueueFull catch to guarantee
    the hot path is never blocked.
    """

    __slots__ = (
        "_queues",
        "_overflow",
        "_flush_limit",
        "_flush_interval_ms",
        "_writer",
        "_tasks",
        "_running",
        "_dropped",
    )

    _TABLE_NAMES: tuple[str, ...] = (
        "audit.orders_log",
        "audit.risk_log",
        "audit.guardrail_log",
    )

    def __init__(
        self,
        queue_size: int | None = None,
        flush_limit: int = _DEFAULT_FLUSH_LIMIT,
        flush_interval_ms: int = _DEFAULT_FLUSH_INTERVAL_MS,
        writer: Any = None,
    ) -> None:
        resolved_size = queue_size or int(os.getenv("HFT_AUDIT_QUEUE_SIZE", str(_DEFAULT_QUEUE_SIZE)))
        overflow_size = int(os.getenv("HFT_AUDIT_OVERFLOW_SIZE", str(_DEFAULT_OVERFLOW_SIZE)))
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {
            name: asyncio.Queue(maxsize=resolved_size) for name in self._TABLE_NAMES
        }
        self._overflow: dict[str, collections.deque[dict[str, Any]]] = {
            name: collections.deque(maxlen=overflow_size) for name in self._TABLE_NAMES
        }
        self._flush_limit = flush_limit
        self._flush_interval_ms = flush_interval_ms
        self._writer = writer
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self._dropped: dict[str, int] = {name: 0 for name in self._TABLE_NAMES}

    # ------------------------------------------------------------------
    # Public logging methods (non-blocking, hot-path safe)
    # ------------------------------------------------------------------

    def log_order(self, order_data: dict[str, Any]) -> None:
        """Log an order dispatch event. Drops silently on queue full."""
        order_data.setdefault("ts_ns", timebase.now_ns())
        self._put("audit.orders_log", order_data)

    def log_risk_decision(self, risk_data: dict[str, Any]) -> None:
        """Log a risk evaluation decision. Drops silently on queue full."""
        risk_data.setdefault("ts_ns", timebase.now_ns())
        self._put("audit.risk_log", risk_data)

    def log_guardrail_transition(self, transition_data: dict[str, Any]) -> None:
        """Log a StormGuard state transition. Drops silently on queue full."""
        transition_data.setdefault("ts_ns", timebase.now_ns())
        self._put("audit.guardrail_log", transition_data)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background flush tasks for each audit table."""
        if self._running:
            return
        self._running = True
        for table_name in self._TABLE_NAMES:
            task = asyncio.create_task(self._flush_loop(table_name))
            self._tasks.append(task)
        logger.info("AuditWriter started", tables=list(self._TABLE_NAMES))

    async def stop(self) -> None:
        """Stop flush tasks and drain remaining rows."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Final drain
        for table_name in self._TABLE_NAMES:
            await self._drain(table_name)
        self._tasks.clear()
        logger.info(
            "AuditWriter stopped",
            dropped_orders=self._dropped.get("audit.orders_log", 0),
            dropped_risk=self._dropped.get("audit.risk_log", 0),
            dropped_guardrail=self._dropped.get("audit.guardrail_log", 0),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _put(self, table: str, data: dict[str, Any]) -> None:
        """Non-blocking enqueue with overflow buffer before drop."""
        try:
            self._queues[table].put_nowait(data)
        except asyncio.QueueFull:
            overflow = self._overflow[table]
            if len(overflow) < overflow.maxlen:  # type: ignore[arg-type]
                overflow.append(data)
                try:
                    from hft_platform.observability.metrics import MetricsRegistry
                    MetricsRegistry.get().audit_overflow_total.labels(table=table).inc()
                except Exception:
                    pass
            else:
                # Overflow also full — hard drop (last resort)
                self._dropped[table] = self._dropped.get(table, 0) + 1
                try:
                    from hft_platform.observability.metrics import MetricsRegistry
                    MetricsRegistry.get().audit_dropped_total.labels(table=table).inc()
                except Exception:
                    pass
                if self._dropped[table] <= 3 or self._dropped[table] % 100 == 0:
                    logger.error(
                        "Audit overflow exhausted, dropping event",
                        table=table,
                        total_dropped=self._dropped[table],
                        overflow_size=len(overflow),
                    )

    async def _flush_loop(self, table_name: str) -> None:
        """Background loop: drain queue and flush in batches."""
        queue = self._queues[table_name]
        batch: list[dict[str, Any]] = []
        interval_s = self._flush_interval_ms / 1000.0

        while self._running:
            try:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=interval_s)
                    batch.append(item)
                except asyncio.TimeoutError:
                    pass

                # Drain available items up to flush_limit
                while len(batch) < self._flush_limit:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if batch:
                    await self._flush_batch(table_name, batch)
                    batch = []

                # Drain overflow deque into main queue after flush frees space
                overflow = self._overflow[table_name]
                while overflow:
                    try:
                        self._queues[table_name].put_nowait(overflow[0])
                        overflow.popleft()
                    except asyncio.QueueFull:
                        break

            except asyncio.CancelledError:
                # Drain remaining before exiting
                if batch:
                    await self._flush_batch(table_name, batch)
                raise
            except Exception as exc:
                logger.error(
                    "Audit flush error",
                    table=table_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    batch_size=len(batch),
                )
                batch = []

    async def _drain(self, table_name: str) -> None:
        """Drain remaining items from queue and overflow, then flush."""
        queue = self._queues[table_name]
        batch: list[dict[str, Any]] = []
        while not queue.empty():
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        # Also drain overflow deque
        overflow = self._overflow[table_name]
        while overflow:
            batch.append(overflow.popleft())
        if batch:
            await self._flush_batch(table_name, batch)

    async def _flush_batch(self, table_name: str, batch: list[dict[str, Any]]) -> None:
        """Write batch to ClickHouse or fall back to structlog."""
        if not batch:
            return

        if self._writer is not None:
            try:
                await self._writer.write(table_name, batch)
                return
            except Exception as exc:
                logger.error(
                    "Audit ClickHouse write failed, falling back to structlog",
                    table=table_name,
                    error=str(exc),
                    batch_size=len(batch),
                )

        # Fallback: log each row via structlog (never lose audit data silently)
        for row in batch:
            logger.info("audit_fallback", table=table_name, **row)

    @property
    def dropped_counts(self) -> dict[str, int]:
        """Return per-table drop counts for observability."""
        return dict(self._dropped)


def get_audit_writer(
    writer: Any = None,
    queue_size: int | None = None,
) -> AuditWriter:
    """Return the singleton AuditWriter, creating it on first call."""
    global _audit_writer
    if _audit_writer is None:
        _audit_writer = AuditWriter(queue_size=queue_size, writer=writer)
    return _audit_writer


def reset_audit_writer() -> None:
    """Reset singleton (for testing)."""
    global _audit_writer
    _audit_writer = None
