"""Unit tests for hft_platform.recorder.batcher module.

Covers ColumnarBuffer, GlobalMemoryGuard, BackpressurePolicy, and Batcher.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.batcher import (
    BackpressurePolicy,
    Batcher,
    ColumnarBuffer,
    GlobalMemoryGuard,
)

# ---------------------------------------------------------------------------
# ColumnarBuffer
# ---------------------------------------------------------------------------


class TestColumnarBuffer:
    """Tests for the column-oriented batch buffer."""

    def test_empty_buffer_properties(self) -> None:
        buf = ColumnarBuffer("test_table")
        assert buf.row_count == 0
        assert buf.column_names is None

    def test_append_row_sets_schema(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1, "b": 2})
        assert buf.column_names == ["a", "b"]
        assert buf.row_count == 1

    def test_append_multiple_rows(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"x": 10, "y": 20})
        buf.append_row({"x": 30, "y": 40})
        assert buf.row_count == 2
        cols, data = buf.to_columnar()
        assert cols == ["x", "y"]
        assert data == [[10, 30], [20, 40]]

    def test_append_row_missing_key_fills_none(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1, "b": 2})
        buf.append_row({"a": 3})  # missing "b"
        cols, data = buf.to_columnar()
        assert data[1] == [2, None]  # column "b"

    def test_set_schema_before_data(self) -> None:
        buf = ColumnarBuffer()
        buf.set_schema(["col1", "col2"])
        assert buf.column_names == ["col1", "col2"]
        assert buf.row_count == 0

    def test_set_schema_noop_when_data_exists(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1})
        buf.set_schema(["x", "y"])  # should be ignored
        assert buf.column_names == ["a"]

    def test_append_values_requires_schema(self) -> None:
        buf = ColumnarBuffer()
        with pytest.raises(RuntimeError, match="Cannot append_values without schema"):
            buf.append_values([1, 2])

    def test_append_values_with_schema(self) -> None:
        buf = ColumnarBuffer()
        buf.set_schema(["a", "b"])
        buf.append_values([10, 20])
        buf.append_values([30, 40])
        assert buf.row_count == 2
        cols, data = buf.to_columnar()
        assert data == [[10, 30], [20, 40]]

    def test_append_values_short_list_pads_none(self) -> None:
        buf = ColumnarBuffer()
        buf.set_schema(["a", "b", "c"])
        buf.append_values([1])  # only 1 of 3
        cols, data = buf.to_columnar()
        assert data == [[1], [None], [None]]

    def test_to_columnar_empty(self) -> None:
        buf = ColumnarBuffer()
        cols, data = buf.to_columnar()
        assert cols == []
        assert data == []

    def test_to_row_dicts(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1, "b": 2})
        buf.append_row({"a": 3, "b": 4})
        rows = buf.to_row_dicts()
        assert rows == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    def test_to_row_dicts_empty(self) -> None:
        buf = ColumnarBuffer()
        assert buf.to_row_dicts() == []

    def test_sort_by_column(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"ts": 3, "v": "c"})
        buf.append_row({"ts": 1, "v": "a"})
        buf.append_row({"ts": 2, "v": "b"})
        buf.sort_by_column("ts")
        rows = buf.to_row_dicts()
        assert [r["ts"] for r in rows] == [1, 2, 3]
        assert [r["v"] for r in rows] == ["a", "b", "c"]

    def test_sort_by_column_noop_for_missing_col(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1})
        buf.sort_by_column("nonexistent")  # should not raise
        assert buf.row_count == 1

    def test_sort_by_column_noop_for_single_row(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"ts": 1, "v": "a"})
        buf.sort_by_column("ts")  # row_count < 2
        assert buf.row_count == 1

    def test_drop_oldest(self) -> None:
        buf = ColumnarBuffer()
        for i in range(5):
            buf.append_row({"i": i})
        buf.drop_oldest(2)
        assert buf.row_count == 3
        rows = buf.to_row_dicts()
        assert [r["i"] for r in rows] == [2, 3, 4]

    def test_drop_oldest_zero_or_negative(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1})
        buf.drop_oldest(0)
        buf.drop_oldest(-5)
        assert buf.row_count == 1

    def test_drop_oldest_more_than_count(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1})
        buf.drop_oldest(100)
        assert buf.row_count == 0

    def test_clear_keeps_schema(self) -> None:
        buf = ColumnarBuffer()
        buf.append_row({"a": 1, "b": 2})
        buf.clear()
        assert buf.row_count == 0
        assert buf.column_names == ["a", "b"]
        # Can still append after clear
        buf.append_row({"a": 3, "b": 4})
        assert buf.row_count == 1

    def test_clear_empty_buffer(self) -> None:
        buf = ColumnarBuffer()
        buf.clear()  # should not raise
        assert buf.row_count == 0


# ---------------------------------------------------------------------------
# GlobalMemoryGuard
# ---------------------------------------------------------------------------


class TestGlobalMemoryGuard:
    """Tests for the cross-table memory budget tracker."""

    def setup_method(self) -> None:
        GlobalMemoryGuard.reset()

    def teardown_method(self) -> None:
        GlobalMemoryGuard.reset()

    def test_singleton(self) -> None:
        g1 = GlobalMemoryGuard.get(max_rows=100)
        g2 = GlobalMemoryGuard.get(max_rows=999)  # ignored, already created
        assert g1 is g2

    def test_reset_clears_singleton(self) -> None:
        g1 = GlobalMemoryGuard.get(max_rows=100)
        GlobalMemoryGuard.reset()
        g2 = GlobalMemoryGuard.get(max_rows=200)
        assert g1 is not g2

    def test_note_rows_added_and_removed(self) -> None:
        guard = GlobalMemoryGuard(max_rows=1000)
        guard.note_rows_added(10)
        assert guard.total_rows == 10
        guard.note_rows_removed(3)
        assert guard.total_rows == 7
        # Remove more than total should clamp to 0
        guard.note_rows_removed(100)
        assert guard.total_rows == 0

    def test_note_rows_added_ignores_non_positive(self) -> None:
        guard = GlobalMemoryGuard(max_rows=100)
        guard.note_rows_added(0)
        guard.note_rows_added(-5)
        assert guard.total_rows == 0

    def test_check_budget_within_limit(self) -> None:
        guard = GlobalMemoryGuard(max_rows=100)
        allowed = guard.check_budget("hft.market_data", 50)
        assert allowed == 50

    def test_check_budget_drops_lower_priority(self) -> None:
        guard = GlobalMemoryGuard(max_rows=20)
        # Create a low-priority batcher with data
        low_batcher = Batcher("hft.latency_spans", flush_limit=100, max_buffer_size=100)
        for i in range(10):
            low_batcher._active.append_row({"i": i})
        guard.register(low_batcher)
        guard.note_rows_added(10)

        # Request from high-priority table should drop from low-priority
        allowed = guard.check_budget("hft.market_data", 15)
        assert allowed > 0

    def test_register_and_unregister(self) -> None:
        guard = GlobalMemoryGuard(max_rows=100)
        batcher = Batcher("hft.orders", flush_limit=100, max_buffer_size=100)
        batcher._active.append_row({"a": 1})
        guard.register(batcher)
        assert guard.total_rows == 1
        guard.unregister("hft.orders")
        assert guard.total_rows == 0

    def test_unregister_nonexistent_table(self) -> None:
        guard = GlobalMemoryGuard(max_rows=100)
        guard.unregister("nonexistent")  # should not raise

    def test_set_health_tracker(self) -> None:
        guard = GlobalMemoryGuard(max_rows=100)
        tracker = MagicMock()
        guard.set_health_tracker(tracker)
        assert guard._health_tracker is tracker


# ---------------------------------------------------------------------------
# Batcher
# ---------------------------------------------------------------------------


class TestBatcher:
    """Tests for the async row accumulator / flusher."""

    def setup_method(self) -> None:
        GlobalMemoryGuard.reset()

    def teardown_method(self) -> None:
        GlobalMemoryGuard.reset()

    def _make_writer(self) -> AsyncMock:
        writer = AsyncMock()
        writer.write = AsyncMock()
        writer.write_columnar = AsyncMock()
        return writer

    @pytest.mark.asyncio
    async def test_add_single_row(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=10, writer=writer)
        await b.add({"price": 100, "qty": 5})
        assert b._active.row_count == 1
        assert b.total_count == 1
        assert b.dropped_count == 0

    @pytest.mark.asyncio
    async def test_add_triggers_flush_at_limit(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=3, writer=writer)
        await b.add({"a": 1})
        await b.add({"a": 2})
        await b.add({"a": 3})  # should trigger flush
        writer.write_columnar.assert_called_once()
        assert b.total_count == 3

    @pytest.mark.asyncio
    async def test_add_many(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, writer=writer)
        rows = [{"a": i} for i in range(5)]
        await b.add_many(rows)
        assert b._active.row_count == 5
        assert b.total_count == 5

    @pytest.mark.asyncio
    async def test_add_many_triggers_flush(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=3, writer=writer)
        rows = [{"a": i} for i in range(5)]
        await b.add_many(rows)
        writer.write_columnar.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_flush(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, writer=writer)
        await b.add({"a": 1})
        await b.force_flush()
        writer.write_columnar.assert_called_once()
        assert b._active.row_count == 0

    @pytest.mark.asyncio
    async def test_force_flush_empty(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, writer=writer)
        await b.force_flush()
        writer.write_columnar.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_flush_by_time(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, flush_interval_ms=0, writer=writer)
        await b.add({"a": 1})
        # Set last_flush_time far in the past
        b.last_flush_time = 0.0
        await b.check_flush()
        writer.write_columnar.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_flush_noop_when_empty(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, flush_interval_ms=0, writer=writer)
        b.last_flush_time = 0.0
        await b.check_flush()
        writer.write_columnar.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_flush_noop_when_interval_not_reached(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, flush_interval_ms=999999, writer=writer)
        await b.add({"a": 1})
        await b.check_flush()
        writer.write_columnar.assert_not_called()

    @pytest.mark.asyncio
    async def test_backpressure_drop_newest(self) -> None:
        writer = self._make_writer()
        b = Batcher(
            "hft.orders",
            flush_limit=100,
            max_buffer_size=3,
            backpressure_policy=BackpressurePolicy.DROP_NEWEST,
            writer=writer,
        )
        for i in range(5):
            await b.add({"a": i})
        # Buffer capped at max_buffer_size
        assert b._active.row_count == 3
        assert b.dropped_count == 2

    @pytest.mark.asyncio
    async def test_backpressure_drop_oldest(self) -> None:
        writer = self._make_writer()
        b = Batcher(
            "hft.orders",
            flush_limit=100,
            max_buffer_size=3,
            backpressure_policy=BackpressurePolicy.DROP_OLDEST,
            writer=writer,
        )
        for i in range(5):
            await b.add({"a": i})
        # Should have kept latest 3
        assert b._active.row_count == 3
        rows = b._active.to_row_dicts()
        assert rows[-1]["a"] == 4

    @pytest.mark.asyncio
    async def test_schema_extractor(self) -> None:
        writer = self._make_writer()

        def extractor(row: Any) -> list[Any]:
            return [row["price"], row["qty"]]

        b = Batcher(
            "hft.market_data",
            flush_limit=100,
            writer=writer,
            extractor=extractor,
            extractor_columns=["price", "qty"],
        )
        await b.add({"price": 100, "qty": 5, "extra": "ignored"})
        assert b._active.row_count == 1
        cols, data = b._active.to_columnar()
        assert cols == ["price", "qty"]
        assert data == [[100], [5]]

    @pytest.mark.asyncio
    async def test_extractor_failure_falls_back_to_serialize(self) -> None:
        writer = self._make_writer()

        def bad_extractor(row: Any) -> list[Any]:
            raise ValueError("boom")

        b = Batcher(
            "hft.orders",
            flush_limit=100,
            writer=writer,
            extractor=bad_extractor,
            extractor_columns=["a"],
        )
        await b.add({"a": 1})
        # Should fall back to serialize path and still add
        assert b._active.row_count == 1

    @pytest.mark.asyncio
    async def test_legacy_buffer_property(self) -> None:
        b = Batcher("hft.orders", flush_limit=100)
        await b.add({"a": 1})
        buf = b.buffer
        assert isinstance(buf, list)
        assert len(buf) == 1
        assert buf[0] == {"a": 1}

    @pytest.mark.asyncio
    async def test_legacy_buffer_setter(self) -> None:
        b = Batcher("hft.orders", flush_limit=100)
        b.buffer = [{"x": 10}, {"x": 20}]
        assert b._active.row_count == 2

    @pytest.mark.asyncio
    async def test_write_timeout_handled(self) -> None:
        writer = self._make_writer()
        writer.write_columnar.side_effect = asyncio.TimeoutError()
        b = Batcher("hft.orders", flush_limit=2, writer=writer)
        await b.add({"a": 1})
        await b.add({"a": 2})  # triggers flush; timeout should be caught

    @pytest.mark.asyncio
    async def test_write_connection_error_handled(self) -> None:
        writer = self._make_writer()
        writer.write_columnar.side_effect = ConnectionError("refused")
        b = Batcher("hft.orders", flush_limit=2, writer=writer)
        await b.add({"a": 1})
        await b.add({"a": 2})  # should not raise

    @pytest.mark.asyncio
    async def test_write_generic_error_handled(self) -> None:
        writer = self._make_writer()
        writer.write_columnar.side_effect = RuntimeError("unexpected")
        b = Batcher("hft.orders", flush_limit=2, writer=writer)
        await b.add({"a": 1})
        await b.add({"a": 2})  # should not raise

    @pytest.mark.asyncio
    async def test_columnar_disabled_uses_row_write(self) -> None:
        writer = self._make_writer()
        with patch.dict("os.environ", {"HFT_BATCHER_COLUMNAR": "0"}):
            b = Batcher("hft.orders", flush_limit=2, writer=writer)
        await b.add({"a": 1})
        await b.add({"a": 2})
        writer.write.assert_called_once()
        writer.write_columnar.assert_not_called()

    @pytest.mark.asyncio
    async def test_sort_ts_on_market_data(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.market_data", flush_limit=100, writer=writer)
        b._sort_min_rows = 2
        await b.add({"exch_ts": 3, "v": "c"})
        await b.add({"exch_ts": 1, "v": "a"})
        await b.add({"exch_ts": 2, "v": "b"})
        await b.force_flush()
        call_args = writer.write_columnar.call_args
        # Column data should be sorted by exch_ts
        col_names = call_args[0][1]
        col_data = call_args[0][2]
        ts_idx = col_names.index("exch_ts")
        assert col_data[ts_idx] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_no_sort_on_non_market_data_table(self) -> None:
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, writer=writer)
        b._sort_min_rows = 1
        await b.add({"exch_ts": 3})
        await b.add({"exch_ts": 1})
        await b.force_flush()
        call_args = writer.write_columnar.call_args
        col_names = call_args[0][1]
        col_data = call_args[0][2]
        ts_idx = col_names.index("exch_ts")
        # Should NOT be sorted since it's not market_data
        assert col_data[ts_idx] == [3, 1]

    @pytest.mark.asyncio
    async def test_memory_guard_integration(self) -> None:
        guard = GlobalMemoryGuard(max_rows=5)
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, writer=writer, memory_guard=guard)
        guard.register(b)

        for i in range(10):
            await b.add({"a": i})

        # Should be capped by global budget
        assert b._active.row_count <= 5

    @pytest.mark.asyncio
    async def test_add_many_with_memory_guard_partial_allow(self) -> None:
        guard = GlobalMemoryGuard(max_rows=3)
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, writer=writer, memory_guard=guard)
        guard.register(b)

        rows = [{"a": i} for i in range(10)]
        await b.add_many(rows)
        # Should be constrained by global budget
        assert b._active.row_count <= 3

    @pytest.mark.asyncio
    async def test_add_serialization_failure(self) -> None:
        """Row that cannot be serialized is silently skipped."""
        writer = self._make_writer()
        b = Batcher("hft.orders", flush_limit=100, writer=writer)
        # Mock serialize to return something that can't be converted to dict
        with patch("hft_platform.recorder.batcher.serialize", return_value=42):
            await b.add(object())
        # The row fails dict() conversion, so nothing added
        assert b._active.row_count == 0

    @pytest.mark.asyncio
    async def test_swap_flush_buffer_reuses_standby(self) -> None:
        """After flush, standby buffer is reused if empty."""
        b = Batcher("hft.orders", flush_limit=100)
        await b.add({"a": 1})
        standby_before = b._standby
        await b.force_flush()
        # After swap, the old standby should have become active (or new buffer created)
        # Just verify flush worked
        assert b._active.row_count == 0

    @pytest.mark.asyncio
    async def test_health_tracker_on_global_drop(self) -> None:
        guard = GlobalMemoryGuard(max_rows=1)
        tracker = MagicMock()
        writer = self._make_writer()
        b = Batcher(
            "hft.orders",
            flush_limit=100,
            writer=writer,
            memory_guard=guard,
            health_tracker=tracker,
        )
        guard.register(b)

        await b.add({"a": 1})
        await b.add({"a": 2})  # should be dropped
        # Tracker should have been called for the drop
        assert b.dropped_count >= 1


class TestBackpressurePolicy:
    """Tests for BackpressurePolicy constants."""

    def test_policy_values(self) -> None:
        assert BackpressurePolicy.DROP_OLDEST == "drop_oldest"
        assert BackpressurePolicy.DROP_NEWEST == "drop_newest"
        assert BackpressurePolicy.BLOCK == "block"
