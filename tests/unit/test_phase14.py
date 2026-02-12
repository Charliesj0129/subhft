"""Unit tests for Phase 14: Quote-to-DB Pipeline Optimization.

Covers: ColumnarBuffer, GlobalMemoryGuard, WALBatchWriter, PipelineHealthTracker,
        schema extractors, double-buffer swap, timestamp sort.
"""

import time

import pytest

# ── CC-1: ColumnarBuffer ─────────────────────────────────────────────────


class TestColumnarBuffer:
    def test_append_row_fixes_schema(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"a": 1, "b": 2})
        assert buf.column_names == ["a", "b"]
        assert buf.row_count == 1

    def test_append_row_multiple(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"x": 10, "y": 20})
        buf.append_row({"x": 30, "y": 40})
        assert buf.row_count == 2
        cols, data = buf.to_columnar()
        assert cols == ["x", "y"]
        assert data == [[10, 30], [20, 40]]

    def test_append_row_missing_key(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"a": 1, "b": 2})
        buf.append_row({"a": 3})  # Missing 'b'
        cols, data = buf.to_columnar()
        assert data[1] == [2, None]  # b column

    def test_append_values_with_schema(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.set_schema(["col1", "col2"])
        buf.append_values([100, 200])
        assert buf.row_count == 1
        cols, data = buf.to_columnar()
        assert data == [[100], [200]]

    def test_append_values_without_schema_raises(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        with pytest.raises(RuntimeError, match="set_schema"):
            buf.append_values([1, 2])

    def test_to_row_dicts(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"a": 1, "b": 2})
        buf.append_row({"a": 3, "b": 4})
        rows = buf.to_row_dicts()
        assert rows == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    def test_clear_keeps_schema(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"a": 1})
        buf.clear()
        assert buf.row_count == 0
        assert buf.column_names == ["a"]
        # Can still append after clear
        buf.append_row({"a": 2})
        assert buf.row_count == 1

    def test_drop_oldest(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        for i in range(5):
            buf.append_row({"v": i})
        buf.drop_oldest(2)
        assert buf.row_count == 3
        rows = buf.to_row_dicts()
        assert [r["v"] for r in rows] == [2, 3, 4]

    def test_empty_buffer(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        cols, data = buf.to_columnar()
        assert cols == []
        assert data == []
        assert buf.to_row_dicts() == []


# ── EC-4: Sort by column ─────────────────────────────────────────────────


class TestColumnarSort:
    def test_sort_by_column(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"ts": 30, "v": "c"})
        buf.append_row({"ts": 10, "v": "a"})
        buf.append_row({"ts": 20, "v": "b"})
        buf.sort_by_column("ts")
        rows = buf.to_row_dicts()
        assert [r["ts"] for r in rows] == [10, 20, 30]
        assert [r["v"] for r in rows] == ["a", "b", "c"]

    def test_sort_with_none_values(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"ts": 20, "v": "b"})
        buf.append_row({"ts": None, "v": "x"})
        buf.append_row({"ts": 10, "v": "a"})
        buf.sort_by_column("ts")
        rows = buf.to_row_dicts()
        # None sorts as 0
        assert rows[0]["v"] == "x"

    def test_sort_nonexistent_column_noop(self):
        from hft_platform.recorder.batcher import ColumnarBuffer

        buf = ColumnarBuffer("test")
        buf.append_row({"a": 1})
        buf.sort_by_column("nonexistent")
        assert buf.row_count == 1  # No crash


# ── CC-2: Double-buffer swap / Batcher ────────────────────────────────────


class _MockWriter:
    def __init__(self):
        self.columnar_calls: list[tuple] = []
        self.legacy_calls: list[tuple] = []

    async def write_columnar(self, table, cols, data, count):
        self.columnar_calls.append((table, cols, data, count))

    async def write(self, table, data):
        self.legacy_calls.append((table, list(data)))


@pytest.mark.asyncio
async def test_batcher_columnar_flush():
    """CC-1/CC-2: Batcher flushes columnar data to writer."""
    from hft_platform.recorder.batcher import Batcher

    w = _MockWriter()
    b = Batcher("hft.test", flush_limit=2, flush_interval_ms=10000, writer=w)

    await b.add({"id": 1, "val": "a"})
    assert not w.columnar_calls

    await b.add({"id": 2, "val": "b"})
    assert len(w.columnar_calls) == 1
    table, cols, data, count = w.columnar_calls[0]
    assert table == "hft.test"
    assert cols == ["id", "val"]
    assert count == 2
    assert data[0] == [1, 2]  # id column
    assert data[1] == ["a", "b"]  # val column


@pytest.mark.asyncio
async def test_batcher_legacy_mode(monkeypatch):
    """With columnar disabled, falls back to row-dict writer."""
    monkeypatch.setenv("HFT_BATCHER_COLUMNAR", "0")
    from hft_platform.recorder.batcher import Batcher

    w = _MockWriter()
    b = Batcher("hft.test", flush_limit=2, flush_interval_ms=10000, writer=w)

    await b.add({"id": 1})
    await b.add({"id": 2})
    assert len(w.legacy_calls) == 1
    assert len(w.columnar_calls) == 0


@pytest.mark.asyncio
async def test_batcher_add_many():
    """CC-5: add_many with dict-based extraction."""
    from hft_platform.recorder.batcher import Batcher

    w = _MockWriter()
    b = Batcher("hft.test", flush_limit=5, flush_interval_ms=10000, writer=w)

    await b.add_many([{"id": i} for i in range(5)])
    assert len(w.columnar_calls) == 1
    _, _, data, count = w.columnar_calls[0]
    assert count == 5


@pytest.mark.asyncio
async def test_batcher_schema_extractor():
    """CC-5: Schema extractor bypasses serialize."""
    from hft_platform.recorder.batcher import Batcher

    calls = []

    def my_extractor(row):
        calls.append(row)
        return {"extracted": True, "val": row}

    w = _MockWriter()
    b = Batcher("hft.test", flush_limit=2, flush_interval_ms=10000, writer=w, extractor=my_extractor)

    await b.add("raw_event_1")
    await b.add("raw_event_2")
    assert len(calls) == 2
    assert len(w.columnar_calls) == 1
    _, cols, _, _ = w.columnar_calls[0]
    assert "extracted" in cols


@pytest.mark.asyncio
async def test_batcher_backpressure_drop_newest():
    """Backpressure still works with columnar buffer."""
    from hft_platform.recorder.batcher import Batcher

    w = _MockWriter()
    b = Batcher(
        "hft.test",
        flush_limit=10,
        flush_interval_ms=10000,
        writer=w,
        max_buffer_size=1,
        backpressure_policy="drop_newest",
    )

    await b.add({"id": 1})
    await b.add({"id": 2})

    assert b.dropped_count == 1
    assert b._active.row_count == 1


@pytest.mark.asyncio
async def test_batcher_backpressure_drop_oldest():
    from hft_platform.recorder.batcher import Batcher

    w = _MockWriter()
    b = Batcher(
        "hft.test",
        flush_limit=10,
        flush_interval_ms=10000,
        writer=w,
        max_buffer_size=1,
        backpressure_policy="drop_oldest",
    )

    await b.add({"id": 1})
    await b.add({"id": 2})

    assert b.dropped_count == 1
    assert b._active.row_count == 1
    rows = b._active.to_row_dicts()
    assert rows[0]["id"] == 2


@pytest.mark.asyncio
async def test_batcher_check_flush():
    from hft_platform.recorder.batcher import Batcher

    w = _MockWriter()
    b = Batcher("hft.test", flush_limit=10, flush_interval_ms=1, writer=w)

    await b.add({"id": 1})
    b.last_flush_time = time.time() - 1

    await b.check_flush()
    assert w.columnar_calls


# ── EC-1: GlobalMemoryGuard ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_global_memory_guard_budget():
    from hft_platform.recorder.batcher import Batcher, GlobalMemoryGuard

    GlobalMemoryGuard.reset()
    guard = GlobalMemoryGuard(max_rows=5)

    w = _MockWriter()
    b1 = Batcher("hft.latency_spans", flush_limit=100, writer=w, memory_guard=guard)
    b2 = Batcher("hft.market_data", flush_limit=100, writer=w, memory_guard=guard)
    guard.register(b1)
    guard.register(b2)

    # Fill low-priority batcher
    for i in range(4):
        await b1.add({"id": i})
    assert b1._active.row_count == 4

    # High-priority should be allowed; low-priority shed
    allowed = guard.check_budget("hft.market_data", 3)
    # After shedding from latency_spans, should allow some
    assert allowed >= 1

    GlobalMemoryGuard.reset()


@pytest.mark.asyncio
async def test_global_memory_guard_under_budget():
    from hft_platform.recorder.batcher import GlobalMemoryGuard

    GlobalMemoryGuard.reset()
    guard = GlobalMemoryGuard(max_rows=100)
    allowed = guard.check_budget("hft.market_data", 10)
    assert allowed == 10
    GlobalMemoryGuard.reset()


# ── CC-4 / EC-3: WALBatchWriter ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_wal_batch_writer_coalescing(tmp_path):
    from hft_platform.recorder.wal import WALBatchWriter

    writer = WALBatchWriter(str(tmp_path))
    writer._timer_running = False  # Disable auto-flush for test

    await writer.add("hft.market_data", [{"symbol": "TSE001", "price": 100}])
    await writer.add("hft.orders", [{"order_id": "O1"}])

    # Manually flush
    await writer.flush()

    files = list(tmp_path.glob("batch_*.jsonl"))
    assert len(files) >= 1

    # Read and verify multi-table format
    import json

    content = files[0].read_text()
    lines = [json.loads(line) for line in content.strip().split("\n")]

    # Should have headers
    headers = [line for line in lines if "__wal_table__" in line]
    assert len(headers) == 2
    assert headers[0]["__wal_table__"] == "hft.market_data"
    assert headers[1]["__wal_table__"] == "hft.orders"

    writer.stop()


@pytest.mark.asyncio
async def test_wal_batch_writer_size_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_WAL_FILE_MAX_MB", "0.001")  # ~1KB limit

    from hft_platform.recorder.wal import WALBatchWriter

    writer = WALBatchWriter(str(tmp_path))
    writer._timer_running = False

    # Add enough data to exceed 1KB
    large_rows = [{"data": "x" * 200} for _ in range(10)]
    await writer.add("hft.test", large_rows)
    await writer.flush()

    files = list(tmp_path.glob("batch_*.jsonl"))
    assert len(files) >= 2, f"Expected file split, got {len(files)} files"

    writer.stop()


# ── EC-5: PipelineHealthTracker ───────────────────────────────────────────


class TestPipelineHealthTracker:
    def test_initial_state_healthy(self):
        from hft_platform.recorder.health import PipelineHealthTracker, PipelineState

        tracker = PipelineHealthTracker()
        assert tracker.state == PipelineState.HEALTHY

    def test_wal_fallback_triggers_degraded(self):
        from hft_platform.recorder.health import PipelineHealthTracker, PipelineState

        tracker = PipelineHealthTracker()
        tracker.record_event("wal_fallback", table="hft.market_data")
        assert tracker.state == PipelineState.DEGRADED

    def test_drops_trigger_degraded(self):
        from hft_platform.recorder.health import PipelineHealthTracker, PipelineState

        tracker = PipelineHealthTracker()
        tracker.record_event("drop", table="hft.latency_spans", count=5)
        assert tracker.state == PipelineState.DEGRADED

    def test_data_loss_triggers_data_loss_state(self):
        from hft_platform.recorder.health import PipelineHealthTracker, PipelineState

        tracker = PipelineHealthTracker()
        tracker.record_event("data_loss", table="hft.market_data", count=100)
        assert tracker.state == PipelineState.DATA_LOSS

    def test_get_health_returns_dict(self):
        from hft_platform.recorder.health import PipelineHealthTracker

        tracker = PipelineHealthTracker()
        tracker.record_event("wal_fallback", table="test")
        health = tracker.get_health()
        assert health["state"] == "DEGRADED"
        assert health["state_value"] == 1
        assert "events_in_window" in health
        assert health["event_counts"]["wal_fallback"] == 1

    def test_prune_removes_old_events(self):
        from hft_platform.recorder.health import PipelineHealthTracker

        tracker = PipelineHealthTracker()
        tracker._window_s = 0.01  # 10ms window
        tracker.record_event("wal_fallback", table="test")
        time.sleep(0.02)
        tracker.prune()
        health = tracker.get_health()
        assert health["events_in_window"] == 0


# ── CC-5: Schema extractors ──────────────────────────────────────────────


class TestSchemaExtractors:
    def test_market_data_extractor_dict(self):
        from hft_platform.recorder.worker import _extract_market_data

        row = {
            "symbol": "TSE001",
            "exchange": "TSE",
            "type": "quote",
            "exch_ts": 1000,
            "ingest_ts": 1001,
            "price_scaled": 50000000,
            "volume": 100,
            "bids_price": [49000000],
            "bids_vol": [10],
            "asks_price": [51000000],
            "asks_vol": [10],
            "seq_no": 42,
        }
        result = _extract_market_data(row)
        assert result is not None
        assert result["symbol"] == "TSE001"
        assert result["price_scaled"] == 50000000
        assert result["seq_no"] == 42

    def test_order_extractor_dict(self):
        from hft_platform.recorder.worker import _extract_order

        row = {"order_id": "O1", "symbol": "TSE001", "side": "buy", "qty": 10}
        result = _extract_order(row)
        assert result is not None
        assert result["order_id"] == "O1"
        assert result["side"] == "buy"

    def test_fill_extractor_dict(self):
        from hft_platform.recorder.worker import _extract_fill

        row = {"trade_id": "T1", "order_id": "O1", "symbol": "TSE001"}
        result = _extract_fill(row)
        assert result is not None
        assert result["trade_id"] == "T1"

    def test_extractor_returns_none_on_error(self):
        from hft_platform.recorder.worker import _extract_market_data

        # Non-dict, non-object with no attributes = returns mostly None values but still works
        result = _extract_market_data(42)
        # Should return a dict (with None values) or None
        assert result is None or isinstance(result, dict)


# ── Loader: Multi-table WAL parsing ──────────────────────────────────────


class TestLoaderBatchParsing:
    def test_parse_batch_table_name(self):
        from hft_platform.recorder.loader import WALLoaderService

        assert WALLoaderService._parse_batch_table_name("hft.market_data") == "market_data"
        assert WALLoaderService._parse_batch_table_name("hft.orders") == "orders"
        assert WALLoaderService._parse_batch_table_name("hft.trades") == "trades"
        assert WALLoaderService._parse_batch_table_name("market_data") == "market_data"
        assert WALLoaderService._parse_batch_table_name("hft.logs") == "risk_log"


# ── Writer: write_columnar ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_writer_wal_fallback_columnar(tmp_path, monkeypatch):
    """write_columnar falls back to WAL when CH is not connected."""
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    monkeypatch.setenv("HFT_DISABLE_CLICKHOUSE", "1")
    monkeypatch.setenv("HFT_WAL_BATCH_ENABLED", "0")

    from hft_platform.recorder.writer import DataWriter

    writer = DataWriter(wal_dir=str(tmp_path))
    writer.connect()

    await writer.write_columnar(
        "hft.orders",
        ["order_id", "qty"],
        [["O1", "O2"], [10, 20]],
        2,
    )

    files = list(tmp_path.glob("hft.orders_*.jsonl"))
    assert files, "WAL file should be written when CH disabled"
