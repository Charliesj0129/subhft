import asyncio
import os
import threading
from typing import Any, Callable, Dict

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.utils.serialization import serialize

logger = get_logger("recorder.batcher")


class BackpressurePolicy:
    """Backpressure handling when buffer is full."""

    DROP_OLDEST = "drop_oldest"  # Drop oldest entries to make room
    DROP_NEWEST = "drop_newest"  # Reject new entries
    BLOCK = "block"  # Block until space available (not recommended for HFT)


class ColumnarBuffer:
    """Column-oriented buffer for batch accumulation (CC-1).

    Stores data as dict[str, list[Any]] (column_name -> values).
    Column order fixed on first row; missing keys get None; extra keys logged+dropped.
    """

    __slots__ = ("_columns", "_column_names", "_row_count", "_table_name")

    def __init__(self, table_name: str = ""):
        self._columns: dict[str, list[Any]] = {}
        self._column_names: list[str] | None = None
        self._row_count: int = 0
        self._table_name = table_name

    @property
    def row_count(self) -> int:
        return self._row_count

    @property
    def column_names(self) -> list[str] | None:
        return self._column_names

    def append_row(self, row_dict: dict[str, Any]) -> None:
        """Append a single row dict to columnar storage."""
        if self._column_names is None:
            # Fix column order from first row
            self._column_names = list(row_dict.keys())
            for col in self._column_names:
                self._columns[col] = []

        for col in self._column_names:
            self._columns[col].append(row_dict.get(col))

        self._row_count += 1

    def append_values(self, values: list[Any]) -> None:
        """Append a pre-ordered values list matching column_names order.

        Used by schema extractors (CC-5) that return values in known column order.
        Requires column_names to be set via set_schema() first.
        """
        if self._column_names is None:
            raise RuntimeError("Cannot append_values without schema set via set_schema()")

        for i, col in enumerate(self._column_names):
            self._columns[col].append(values[i] if i < len(values) else None)

        self._row_count += 1

    def set_schema(self, column_names: list[str]) -> None:
        """Pre-set column schema (used with schema extractors)."""
        if self._column_names is not None and self._row_count > 0:
            return  # Already has data, don't reset
        self._column_names = list(column_names)
        self._columns = {col: [] for col in self._column_names}

    def to_columnar(self) -> tuple[list[str], list[list[Any]]]:
        """Return (column_names, column_data) for CH insert."""
        if not self._column_names or self._row_count == 0:
            return [], []
        return list(self._column_names), [self._columns[col] for col in self._column_names]

    def to_row_dicts(self) -> list[dict[str, Any]]:
        """Reconstruct row dicts (cold path, WAL fallback)."""
        if not self._column_names or self._row_count == 0:
            return []
        result = []
        for i in range(self._row_count):
            row = {}
            for col in self._column_names:
                row[col] = self._columns[col][i]
            result.append(row)
        return result

    def sort_by_column(self, col_name: str) -> None:
        """Sort all columns by the values of the specified column (EC-4)."""
        if col_name not in self._columns or self._row_count < 2:
            return
        indices = sorted(range(self._row_count), key=lambda i: self._columns[col_name][i] or 0)
        for col in self._column_names:  # type: ignore[union-attr]
            old = self._columns[col]
            self._columns[col] = [old[i] for i in indices]

    def drop_oldest(self, count: int) -> None:
        """Drop the oldest N rows."""
        if count <= 0 or not self._column_names:
            return
        count = min(count, self._row_count)
        for col in self._column_names:
            self._columns[col] = self._columns[col][count:]
        self._row_count -= count

    def clear(self) -> None:
        """Reset buffer to empty state, keeping schema."""
        if self._column_names:
            for col in self._column_names:
                self._columns[col] = []
        self._row_count = 0


class GlobalMemoryGuard:
    """Cross-table memory budget tracker (EC-1).

    Tracks total buffered rows across all batchers. When budget exceeded,
    lowest-priority batchers drop first.
    """

    # Priority: higher = more important (market_data most important)
    DEFAULT_PRIORITIES: dict[str, int] = {
        "hft.market_data": 100,
        "hft.orders": 90,
        "hft.trades": 80,
        "hft.risk_log": 50,  # Also matches hft.logs
        "hft.logs": 50,
        "hft.backtest_runs": 30,
        "hft.latency_spans": 10,
    }

    _instance: "GlobalMemoryGuard | None" = None
    _lock = threading.Lock()

    def __init__(self, max_rows: int | None = None):
        self._max_rows = max_rows or int(os.getenv("HFT_GLOBAL_BUFFER_MAX_ROWS", "50000"))
        self._batchers: dict[str, "Batcher"] = {}
        self._total_rows = 0
        self._health_tracker: Any = None
        self._state_lock = threading.Lock()

    @classmethod
    def get(cls, max_rows: int | None = None) -> "GlobalMemoryGuard":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(max_rows)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None

    def set_health_tracker(self, tracker: Any) -> None:
        self._health_tracker = tracker

    def register(self, batcher: "Batcher") -> None:
        with self._state_lock:
            self._batchers[batcher.table_name] = batcher
            self._total_rows += int(batcher._active.row_count)

    def unregister(self, table_name: str) -> None:
        with self._state_lock:
            batcher = self._batchers.pop(table_name, None)
            if batcher is not None:
                self._total_rows = max(0, self._total_rows - int(batcher._active.row_count))

    @property
    def total_rows(self) -> int:
        with self._state_lock:
            return self._total_rows

    def note_rows_added(self, count: int) -> None:
        if count <= 0:
            return
        with self._state_lock:
            self._total_rows += int(count)

    def note_rows_removed(self, count: int) -> None:
        if count <= 0:
            return
        with self._state_lock:
            self._total_rows = max(0, self._total_rows - int(count))

    def check_budget(self, requesting_table: str, additional_rows: int) -> int:
        """Check if adding rows would exceed budget.

        Returns number of rows allowed (may be less than requested).
        If budget exceeded, drops from lowest-priority batchers first.
        """
        drop_events: list[tuple[str, int, int]] = []
        with self._state_lock:
            total = self._total_rows
            if total + additional_rows <= self._max_rows:
                return additional_rows

            excess = (total + additional_rows) - self._max_rows
            req_priority = self.DEFAULT_PRIORITIES.get(requesting_table, 0)
            sorted_batchers = sorted(
                self._batchers.items(),
                key=lambda x: self.DEFAULT_PRIORITIES.get(x[0], 0),
            )

            for name, batcher in sorted_batchers:
                if excess <= 0:
                    break
                b_priority = self.DEFAULT_PRIORITIES.get(name, 0)
                if b_priority >= req_priority:
                    continue
                available = batcher._active.row_count
                if available <= 0:
                    continue
                drop = min(excess, available)
                batcher._active.drop_oldest(drop)
                batcher.dropped_count += drop
                self._total_rows = max(0, self._total_rows - drop)
                excess -= drop
                drop_events.append((name, drop, b_priority))

            allowed = max(0, self._max_rows - self._total_rows)
            allowed_rows = min(additional_rows, allowed)

        for name, drop, b_priority in drop_events:
            logger.warning(
                "Global memory guard: dropped rows from lower-priority batcher",
                table=name,
                dropped=drop,
                priority=b_priority,
            )
            if self._health_tracker:
                self._health_tracker.record_event("drop", table=name, count=drop)

        return allowed_rows


class Batcher:
    """
    Accumulates rows per table in columnar format (CC-1).
    Uses double-buffer swap for lock-free flushing (CC-2).
    Supports schema extractors for hot-path optimization (CC-5).
    Implements backpressure handling to prevent unbounded memory growth.
    """

    # Maximum buffer size before backpressure kicks in
    DEFAULT_MAX_BUFFER_SIZE = 10000

    def _new_buffer(self) -> ColumnarBuffer:
        buf = ColumnarBuffer(self.table_name)
        cols = getattr(self, "_extractor_columns", None)
        if cols:
            buf.set_schema(cols)
        return buf

    def __init__(
        self,
        table_name: str,
        flush_limit: int = 1000,
        flush_interval_ms: int = 500,
        writer=None,
        max_buffer_size: int | None = None,
        backpressure_policy: str = BackpressurePolicy.DROP_NEWEST,
        extractor: Callable | None = None,
        extractor_columns: list[str] | None = None,
        memory_guard: GlobalMemoryGuard | None = None,
        health_tracker: Any = None,
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

        # CC-1: Columnar buffers with CC-2 double-buffer swap
        self._active = self._new_buffer()
        self._standby = self._new_buffer()
        self.last_flush_time = timebase.now_s()
        self.lock = asyncio.Lock()

        # CC-5: Schema extractor
        self._extractor = extractor
        self._extractor_columns = list(extractor_columns) if extractor_columns else None
        if self._extractor_columns:
            self._active.set_schema(self._extractor_columns)
            self._standby.set_schema(self._extractor_columns)
        self._columnar_enabled = os.getenv("HFT_BATCHER_COLUMNAR", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._extract_enabled = os.getenv("HFT_BATCHER_SCHEMA_EXTRACT", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # EC-4: Timestamp sort
        self._sort_ts_enabled = os.getenv("HFT_BATCHER_SORT_TS", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._sort_min_rows = int(os.getenv("HFT_BATCHER_SORT_MIN_ROWS", "50"))
        # Only sort market_data by default
        self._sort_ts_tables = {"hft.market_data"}

        # EC-1: Global memory guard
        self._memory_guard = memory_guard

        # EC-5: Health tracker
        self._health_tracker = health_tracker

        # Metrics for monitoring backpressure events
        self.dropped_count = 0
        self.total_count = 0

        # Legacy compat: buffer property
        # (tests may access batcher.buffer directly)

    @property
    def buffer(self) -> list[dict[str, Any]]:
        """Legacy compatibility: return active buffer as row dicts."""
        return self._active.to_row_dicts()

    @buffer.setter
    def buffer(self, value: list[dict[str, Any]]) -> None:
        """Legacy compatibility: set buffer from row dicts."""
        self._active.clear()
        for row in value:
            self._active.append_row(row)

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

    def _extract_row(self, row: Any) -> dict[str, Any] | list[Any] | None:
        """Use schema extractor if available (CC-5), else fall back to serialize."""
        if self._extract_enabled and self._extractor is not None:
            try:
                result = self._extractor(row)
                if result is not None:
                    return result
            except Exception:
                pass  # Fall through to generic
        return self._serialize_row(row)

    def _add_to_active(self, extracted: dict[str, Any] | list[Any]) -> None:
        """Add extracted data to active columnar buffer."""
        if isinstance(extracted, dict):
            self._active.append_row(extracted)
        elif isinstance(extracted, (list, tuple)):
            self._active.append_values(extracted)

    async def add(self, row: Any):
        extracted = self._extract_row(row)
        if extracted is None:
            return

        flush_buf: ColumnarBuffer | None = None
        async with self.lock:
            self.total_count += 1

            # EC-1: Check global memory budget
            if self._memory_guard is not None:
                allowed = self._memory_guard.check_budget(self.table_name, 1)
                if allowed <= 0:
                    self.dropped_count += 1
                    if self._health_tracker:
                        self._health_tracker.record_event("drop", table=self.table_name, count=1)
                    return

            # Local backpressure
            if self._active.row_count >= self.max_buffer_size:
                self._apply_backpressure_locked(1)
                if self._active.row_count >= self.max_buffer_size:
                    return  # DROP_NEWEST

            self._add_to_active(extracted)
            if self._memory_guard is not None:
                self._memory_guard.note_rows_added(1)

            if self._active.row_count >= self.flush_limit:
                flush_buf = self._swap_flush_buffer_locked()

        if flush_buf is not None:
            await self._write_flush_buffer(flush_buf)

    async def add_many(self, rows: list[Any]):
        """Add multiple rows under a single lock acquisition (CC-5)."""
        extracted_list = []
        for row in rows:
            ex = self._extract_row(row)
            if ex is not None:
                extracted_list.append(ex)
        if not extracted_list:
            return

        flush_buf: ColumnarBuffer | None = None
        async with self.lock:
            self.total_count += len(extracted_list)

            # EC-1: Check global memory budget
            if self._memory_guard is not None:
                allowed = self._memory_guard.check_budget(self.table_name, len(extracted_list))
                if allowed < len(extracted_list):
                    self.dropped_count += len(extracted_list) - allowed
                    extracted_list = extracted_list[:allowed] if allowed > 0 else []
                    if not extracted_list:
                        return

            available = self.max_buffer_size - self._active.row_count
            if available < len(extracted_list):
                self._apply_backpressure_locked(len(extracted_list) - available)
                available = self.max_buffer_size - self._active.row_count
                extracted_list = extracted_list[:available] if available > 0 else []

            for ex in extracted_list:
                self._add_to_active(ex)
            if self._memory_guard is not None:
                self._memory_guard.note_rows_added(len(extracted_list))

            if self._active.row_count >= self.flush_limit:
                flush_buf = self._swap_flush_buffer_locked()

        if flush_buf is not None:
            await self._write_flush_buffer(flush_buf)

    def _apply_backpressure_locked(self, overflow_count: int) -> None:
        """Handle backpressure when buffer is at capacity."""
        if self._active.row_count < self.max_buffer_size:
            return
        if self.backpressure_policy == BackpressurePolicy.DROP_OLDEST:
            drop_count = max(overflow_count, self._active.row_count - self.max_buffer_size + 1)
            self._active.drop_oldest(drop_count)
            if self._memory_guard is not None:
                self._memory_guard.note_rows_removed(drop_count)
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
        flush_buf: ColumnarBuffer | None = None
        async with self.lock:
            if self._active.row_count == 0:
                return

            age = (timebase.now_s() - self.last_flush_time) * 1000
            if age >= self.flush_interval_ms:
                flush_buf = self._swap_flush_buffer_locked()

        if flush_buf is not None:
            await self._write_flush_buffer(flush_buf)

    async def force_flush(self):
        """Manually trigger flush (e.g. shutdown)."""
        flush_buf: ColumnarBuffer | None = None
        async with self.lock:
            flush_buf = self._swap_flush_buffer_locked()
        if flush_buf is not None:
            await self._write_flush_buffer(flush_buf)

    def _swap_flush_buffer_locked(self) -> ColumnarBuffer | None:
        """Swap active/standby under lock and return buffer for out-of-lock write."""
        if self._active.row_count == 0:
            return None

        # Swap active <-> standby (pointer swap under lock)
        flush_buf = self._active
        next_active = self._standby if self._standby.row_count == 0 else self._new_buffer()
        self._active = next_active
        self._standby = flush_buf
        # Clear active for new writes (keeps schema)
        self._active.clear()
        self.last_flush_time = timebase.now_s()
        if self._memory_guard is not None:
            self._memory_guard.note_rows_removed(flush_buf.row_count)
        return flush_buf

    async def _write_flush_buffer(self, flush_buf: ColumnarBuffer) -> None:
        if self.writer:
            try:
                if (
                    self._sort_ts_enabled
                    and self.table_name in self._sort_ts_tables
                    and flush_buf.row_count >= self._sort_min_rows
                ):
                    flush_buf.sort_by_column("exch_ts")

                if self._columnar_enabled:
                    cols, data = flush_buf.to_columnar()
                    if cols and data:
                        await self.writer.write_columnar(self.table_name, cols, data, flush_buf.row_count)
                else:
                    data = flush_buf.to_row_dicts()
                    if data:
                        await self.writer.write(self.table_name, data)
            except asyncio.TimeoutError:
                logger.error(
                    "Write timeout - data written to WAL",
                    table=self.table_name,
                    count=flush_buf.row_count,
                )
            except ConnectionError as e:
                logger.error(
                    "Connection error during write",
                    table=self.table_name,
                    error=str(e),
                    count=flush_buf.row_count,
                )
            except Exception as e:
                logger.error(
                    "Write failed",
                    table=self.table_name,
                    error=str(e),
                    error_type=type(e).__name__,
                    count=flush_buf.row_count,
                )
        flush_buf.clear()
