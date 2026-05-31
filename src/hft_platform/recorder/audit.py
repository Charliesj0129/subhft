"""Audit table writers for orders, risk decisions, and guardrail transitions.

Non-blocking audit logging via bounded asyncio.Queue with drop-on-full semantics.
Background flush tasks batch rows and write to ClickHouse audit tables.
Falls back to structlog if ClickHouse is unavailable.

P0-I3 (2026-04-24): asyncio.Queue instances are created lazily inside ``start()``
so that the queues bind to the engine loop. Previously they were created in
``__init__`` — the first caller to hit ``log_*`` could be a daemon thread (e.g.
bootstrap lease-refresh → storm_guard → audit), which bound queue futures to an
orphan loop and silently dropped audit rows. Pre-start log calls buffer into the
thread-safe ``_pre_start_buffer`` (a ``collections.deque`` under a lock); the
buffer is drained into the real queues once ``start()`` runs on the engine loop.

P1 guardrail retention: ``audit.guardrail_log`` uses a "sticky-first" overflow
policy — when the queue + overflow are full, NEW entries are dropped instead of
evicting the oldest. The first transition (root cause of a cascade) is the most
valuable row to retain for post-incident forensics.
"""

from __future__ import annotations

import asyncio
import collections
import os
import threading
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("recorder.audit")

_DEFAULT_QUEUE_SIZE = 10_000
_DEFAULT_FLUSH_INTERVAL_MS = 1_000
_DEFAULT_FLUSH_LIMIT = 500
_DEFAULT_OVERFLOW_SIZE = 50_000
# P1 fix: guardrail_log rows (StormGuard state transitions) are operationally
# critical — root-cause transitions must survive a bursty cascade. Larger default
# overflow, and "sticky-first" policy applied in ``_put``.
_DEFAULT_GUARDRAIL_OVERFLOW_SIZE = 100_000
_GUARDRAIL_TABLE = "audit.guardrail_log"

# P1-a (2026-04-27): canonical column schemas for each audit table.
# Producers emit different optional fields per call site (e.g. AMEND uses
# new_price, CANCEL uses target_key, dispatch_failed uses error). The
# ClickHouse insert path (DataWriter._ch_insert) infers columns from the
# first row of the batch and would otherwise silently drop fields present in
# later rows. ``_normalize_row`` fills missing keys with type-appropriate
# defaults so every batched row has the same shape, matching the DDL in
# 20260427_001_audit_schema_alignment.sql.
_AUDIT_SCHEMA_DEFAULTS: dict[str, dict[str, Any]] = {
    "audit.orders_log": {
        "ts_ns": 0,
        "event": "",
        "intent_type": "",
        "order_key": "",
        "target_key": "",
        "symbol": "",
        "side": "",
        "price": 0,
        "new_price": 0,
        "qty": 0,
        "strategy_id": "",
        "cmd_id": 0,
        "error": "",
        "details": "",
    },
    "audit.risk_log": {
        "ts_ns": 0,
        "strategy_id": "",
        "symbol": "",
        "intent_type": 0,
        "price": 0,
        "qty": 0,
        "approved": 0,
        "reason_code": "",
    },
    "audit.guardrail_log": {
        "ts_ns": 0,
        "old_state": "",
        "new_state": "",
        "reason": "",
    },
}


def _normalize_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """Return a row dict matching the canonical schema for ``table``.

    Missing keys are filled with type-appropriate defaults; extra keys are
    JSON-encoded into ``details`` (orders_log only — other tables drop them
    to avoid schema drift). ``approved`` (bool→UInt8) is coerced if present.
    """
    schema = _AUDIT_SCHEMA_DEFAULTS.get(table)
    if schema is None:
        return row
    out = dict(schema)
    extras: dict[str, Any] = {}
    for k, v in row.items():
        if k in schema:
            if k == "approved":
                out[k] = 1 if bool(v) else 0
            else:
                out[k] = v
        else:
            extras[k] = v
    if extras and "details" in schema:
        try:
            import json

            out["details"] = json.dumps(extras, default=str, separators=(",", ":"))
        except Exception:  # noqa: BLE001 — never crash the audit path on json
            out["details"] = ""
    return out


# Bug #30: structlog method signature is `meth(event, *args, **kw)` plus a few
# other reserved kwargs it injects. Splatting a row dict containing any of these
# keys raises TypeError. Rename to `row_*` prefix in fallback path.
_RESERVED_STRUCTLOG_KEYS: frozenset[str] = frozenset(
    {"event", "exc_info", "stack_info", "level", "logger", "timestamp", "_record", "_from_structlog"}
)

# Singleton instance
_audit_writer: AuditWriter | None = None


class RecorderQueueAuditWriter:
    """AuditWriter persistence sink for recorder WAL-first mode.

    It matches the DataWriter ``write(table, rows)`` interface but sends the
    normalized audit batch into RecorderService's queue. The recorder then uses
    its configured mode, so WAL-first audit writes do not touch the direct
    ClickHouse DataWriter or its health tracker.
    """

    __slots__ = ("_queue",)

    def __init__(self, queue: "asyncio.Queue[dict[str, Any]]") -> None:
        self._queue = queue

    async def write(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        await self._queue.put({"topic": table, "data": list(rows)})


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
        # P0-I3: Pre-start thread-safe buffer used until ``start()`` creates the
        # real asyncio.Queue instances on the engine loop.
        "_pre_start_buffer",
        "_pre_start_lock",
        "_queues_ready",
        "_queue_size",
        "_overflow_size",
        # I-M2 (2026-04-25): cross-thread fallback. _loop and _loop_thread_id
        # are captured in ``start()``; _put detects non-loop callers and
        # schedules the actual queue mutation via call_soon_threadsafe.
        "_loop",
        "_loop_thread_id",
        "_cross_thread_count",
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
        guardrail_overflow_size = int(
            os.getenv("HFT_AUDIT_GUARDRAIL_OVERFLOW_SIZE", str(_DEFAULT_GUARDRAIL_OVERFLOW_SIZE))
        )
        self._queue_size = resolved_size
        # Per-table overflow sizes — guardrail gets a larger allocation so
        # root-cause transitions survive bursty cascades.
        self._overflow_size: dict[str, int] = {
            name: (guardrail_overflow_size if name == _GUARDRAIL_TABLE else overflow_size) for name in self._TABLE_NAMES
        }

        # P0-I3: DO NOT create asyncio.Queue instances here. The engine loop is
        # not yet running when AuditWriter is first constructed (see
        # bootstrap.py / HFTSystem.__init__ ordering). Queues are created lazily
        # in ``start()`` on the engine loop.
        self._queues: dict[str, asyncio.Queue[dict[str, Any]] | None] = dict.fromkeys(self._TABLE_NAMES, None)
        self._queues_ready = False

        # Pre-start buffer: thread-safe (collections.deque + threading.Lock)
        # so any thread (including the daemon lease-refresh thread that may
        # invoke StormGuard._transition before the engine loop is running) can
        # safely enqueue audit rows without corrupting an asyncio.Queue. Size is
        # queue_size + overflow_size so pre-start cannot exhaust earlier than
        # the post-start queue + overflow combination would have.
        self._pre_start_lock = threading.Lock()
        self._pre_start_buffer: dict[str, collections.deque[dict[str, Any]]] = {
            name: collections.deque(maxlen=self._queue_size + self._overflow_size[name]) for name in self._TABLE_NAMES
        }
        # Post-start overflow deque (per-table sized).
        self._overflow: dict[str, collections.deque[dict[str, Any]]] = {
            name: collections.deque(maxlen=self._overflow_size[name]) for name in self._TABLE_NAMES
        }
        self._flush_limit = flush_limit
        self._flush_interval_ms = flush_interval_ms
        self._writer = writer
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self._dropped: dict[str, int] = {name: 0 for name in self._TABLE_NAMES}
        # I-M2: cross-thread bookkeeping. Captured in start(); read in _put.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._cross_thread_count: int = 0

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
        """Start background flush tasks for each audit table.

        P0-I3: Creates the asyncio.Queue instances on the current running loop
        (engine loop) and drains any rows accumulated in ``_pre_start_buffer``
        by threads that logged before the loop was up. MUST be called from the
        engine loop.
        """
        if self._running:
            return

        # I-M2: capture the engine loop + its thread id so _put can detect
        # cross-thread callers (lease-refresh-thread → trigger_halt → audit,
        # broker-callback-thread → trigger_storm → audit) and dispatch via
        # call_soon_threadsafe instead of corrupting the loop's _ready deque.
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()

        # Create queues on the engine loop (getter semantics in Python 3.12 —
        # asyncio.Queue binds to the running loop on first put/get).
        for table_name in self._TABLE_NAMES:
            self._queues[table_name] = asyncio.Queue(maxsize=self._queue_size)
        self._queues_ready = True

        # Drain pre-start buffer into the fresh queues under lock.
        with self._pre_start_lock:
            for table_name, buf in self._pre_start_buffer.items():
                q = self._queues[table_name]
                if q is None:
                    continue
                while buf:
                    item = buf.popleft()
                    try:
                        q.put_nowait(item)
                    except asyncio.QueueFull:
                        # Queue already full from pre-start overflow — route
                        # into the normal overflow deque for retry on flush.
                        overflow = self._overflow[table_name]
                        if overflow.maxlen is None or len(overflow) < overflow.maxlen:
                            overflow.append(item)
                        else:
                            self._dropped[table_name] = self._dropped.get(table_name, 0) + 1

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
        # I-M2: clear loop binding so any late-arriving log_* call from a
        # daemon thread falls back to overflow deque rather than scheduling
        # on a closed loop.
        self._loop = None
        self._loop_thread_id = None
        logger.info(
            "AuditWriter stopped",
            dropped_orders=self._dropped.get("audit.orders_log", 0),
            dropped_risk=self._dropped.get("audit.risk_log", 0),
            dropped_guardrail=self._dropped.get("audit.guardrail_log", 0),
            cross_thread_count=self._cross_thread_count,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _put(self, table: str, data: dict[str, Any]) -> None:
        """Non-blocking enqueue with overflow buffer before drop.

        P0-I3: Before ``start()`` binds asyncio.Queue instances, all writes land
        in ``_pre_start_buffer`` (thread-safe deque under a lock). This lets a
        daemon thread safely enqueue StormGuard transition rows during startup.

        I-M2 (2026-04-25): post-start cross-thread callers (lease-refresh
        thread, broker-callback thread) cannot safely call ``q.put_nowait``
        directly — asyncio.Queue is documented not thread-safe (it calls
        ``loop.call_soon`` internally). Detect non-loop thread context and
        dispatch the actual queue mutation via ``loop.call_soon_threadsafe``.

        P1 sticky-first (guardrail only): when queue + overflow are both full,
        the ``audit.guardrail_log`` table drops the NEW entry instead of evicting
        the oldest — the root-cause transition at the top of a cascade is the
        most valuable row to retain for forensics.
        """
        # Pre-start path: queues not yet created on any loop.
        if not self._queues_ready:
            with self._pre_start_lock:
                buf = self._pre_start_buffer[table]
                if buf.maxlen is None or len(buf) < buf.maxlen:
                    buf.append(data)
                else:
                    # Guardrail: reject newest to preserve oldest (root cause).
                    if table == _GUARDRAIL_TABLE:
                        self._dropped[table] = self._dropped.get(table, 0) + 1
                    else:
                        # Non-guardrail: retain newest, evict oldest. Counted
                        # as a drop for observability (an entry IS being lost,
                        # even if deque auto-manages the maxlen).
                        buf.append(data)  # deque drops leftmost automatically
                        self._dropped[table] = self._dropped.get(table, 0) + 1
            return

        # I-M2: cross-thread detection. If we are not on the loop thread,
        # schedule the actual put on the loop. The fast path (loop thread)
        # avoids the call_soon_threadsafe overhead.
        loop = self._loop
        if loop is not None and threading.get_ident() != self._loop_thread_id:
            self._cross_thread_count += 1
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                MetricsRegistry.get().audit_put_cross_thread_total.labels(table=table).inc()
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.call_soon_threadsafe(self._do_put, table, data)
            except RuntimeError:
                # Loop closed/closing — fall back to overflow deque (still
                # thread-safe via deque's append) so audit isn't lost.
                overflow = self._overflow[table]
                if overflow.maxlen is not None and len(overflow) < overflow.maxlen:
                    overflow.append(data)
                else:
                    self._dropped[table] = self._dropped.get(table, 0) + 1
            return

        # Loop-thread path: existing fast path.
        self._do_put(table, data)

    def _do_put(self, table: str, data: dict[str, Any]) -> None:
        """Actual queue mutation. MUST run on the loop thread.

        Called directly from _put on the loop thread, or scheduled via
        ``loop.call_soon_threadsafe`` from a non-loop thread.
        """
        q = self._queues[table]
        if q is None:
            # Should not happen after _queues_ready — defensive fallback.
            self._dropped[table] = self._dropped.get(table, 0) + 1
            return

        try:
            q.put_nowait(data)
            return
        except asyncio.QueueFull:
            pass

        overflow = self._overflow[table]
        if overflow.maxlen is not None and len(overflow) < overflow.maxlen:
            overflow.append(data)
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                MetricsRegistry.get().audit_overflow_total.labels(table=table).inc()
            except Exception:
                pass
            return

        # Queue + overflow both full.
        if table == _GUARDRAIL_TABLE:
            # P1 sticky-first: do NOT evict oldest; drop this new event so
            # the first (root-cause) transition is preserved.
            self._dropped[table] = self._dropped.get(table, 0) + 1
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                MetricsRegistry.get().audit_dropped_total.labels(table=table).inc()
            except Exception:
                pass
            if self._dropped[table] <= 3 or self._dropped[table] % 100 == 0:
                logger.error(
                    "Audit guardrail overflow exhausted (sticky-first), dropping NEWEST",
                    table=table,
                    total_dropped=self._dropped[table],
                    overflow_size=len(overflow),
                )
            return

        # Non-guardrail tables: hard drop (last resort) — behaviour preserved
        # from prior revision for backward compat.
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
        if queue is None:
            # Defensive — should never happen because start() populates queues
            # before scheduling flush loops.
            logger.error("audit_flush_loop_no_queue", table=table_name)
            return
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

                # Drain overflow deque into main queue after flush frees space.
                # P3-a2 (2026-04-27): self._queues[table_name] is typed
                # ``Queue | None`` because stop() clears the queue ref. When
                # _flush_loop runs concurrently with stop(), accessing
                # ``.put_nowait`` on None would raise AttributeError. Treat
                # that race as queue-closed and increment a labeled drop
                # counter so we know about it instead of crashing the loop.
                overflow = self._overflow[table_name]
                while overflow:
                    cur_queue = self._queues[table_name]
                    if cur_queue is None:
                        # Queue cleared mid-loop — abandon overflow drain;
                        # _drain() will collect from overflow at shutdown.
                        try:
                            from hft_platform.observability.metrics import MetricsRegistry

                            MetricsRegistry.get().audit_dropped_total.labels(table=table_name).inc(len(overflow))
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    try:
                        cur_queue.put_nowait(overflow[0])
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
        if queue is not None:
            while not queue.empty():
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        # Also drain overflow deque
        overflow = self._overflow[table_name]
        while overflow:
            batch.append(overflow.popleft())
        # P0-I3: also drain pre-start buffer if anything is still there
        # (e.g. shutdown before start() completed).
        with self._pre_start_lock:
            buf = self._pre_start_buffer[table_name]
            while buf:
                batch.append(buf.popleft())
        if batch:
            await self._flush_batch(table_name, batch)

    async def _flush_batch(self, table_name: str, batch: list[dict[str, Any]]) -> None:
        """Write batch to ClickHouse or fall back to structlog."""
        if not batch:
            return

        if self._writer is not None:
            # P1-a (2026-04-27): normalize rows to the canonical DDL schema
            # before handing to the writer. Each producer call site emits a
            # different subset of optional fields; without normalization the
            # writer's "infer columns from first row" path silently drops
            # fields present only in later rows. See _normalize_row docstring.
            normalized = [_normalize_row(table_name, row) for row in batch]
            try:
                await self._writer.write(table_name, normalized)
                return
            except Exception as exc:
                # P1-a: surface persistence failures via a labeled metric so
                # ops dashboards can detect audit-write degradation without
                # tailing structlog. ``reason`` keeps cardinality bounded by
                # using the exception class name (not the full message).
                try:
                    from hft_platform.observability.metrics import MetricsRegistry

                    MetricsRegistry.get().audit_persist_failures_total.labels(
                        table=table_name, reason=type(exc).__name__
                    ).inc()
                except Exception:  # noqa: BLE001
                    pass
                logger.error(
                    "Audit ClickHouse write failed, falling back to structlog",
                    table=table_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    batch_size=len(batch),
                )

        # Fallback: log each row via structlog (never lose audit data silently).
        # Bug #30: row dicts may contain keys reserved by structlog (`event`,
        # `timestamp`, `level`, etc). Splatting them collides with structlog's
        # positional kwargs → TypeError → batch dropped. Rename to row_* prefix.
        # Log level rationale: when `self._writer is None` this is the documented
        # operating mode (Bug #19) with a single startup warning — downgrade
        # per-row emission to DEBUG to avoid 8 k/day INFO spam. When a writer
        # *was* configured but the write actually failed, the `logger.error`
        # above already fired; per-row rows here still help forensic recovery.
        row_level = logger.debug if self._writer is None else logger.info
        for row in batch:
            safe_row = {(f"row_{k}" if k in _RESERVED_STRUCTLOG_KEYS else k): v for k, v in row.items()}
            row_level("audit_fallback", table=table_name, **safe_row)

    @property
    def dropped_counts(self) -> dict[str, int]:
        """Return per-table drop counts for observability."""
        return dict(self._dropped)

    def set_writer(self, writer: Any) -> None:
        """Attach (or replace) the ClickHouse-bound writer after construction.

        P1-a (2026-04-27): the singleton ``AuditWriter`` is created lazily on
        the first ``log_*`` call (often from a daemon thread before the engine
        loop is up — see P0-I3 in the module docstring). The recorder's
        ``DataWriter`` only becomes available later in startup. ``set_writer``
        lets ``services.system._run_internal`` attach the recorder writer once
        both objects exist, so audit rows actually land in ClickHouse instead
        of falling through to the structlog ``audit_fallback`` path.
        """
        self._writer = writer


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
