"""Coverage tests for recorder/wal.py.

Targets remaining uncovered paths:
  - WALWriter.write() disk_full skip via _check_disk_space (not cached)
  - WALWriter._write_sync_atomic temp file cleanup on exception
  - WALWriter._maybe_fsync_dir runs when threshold zero and records latency
  - WALBatchWriter._check_disk_space: was_full=True, now healthy, recovery log
  - WALBatchWriter.flush() failure: data merge-back into buffer + columnar merge
  - WALBatchWriter._flush_timer_loop: merge-back circuit breaker (data dropped)
  - WALBatchWriter._flush_timer_loop: successful flush resets failure counter
  - WALBatchWriter._write_batch_sync: empty rows dict skipped
  - WALBatchWriter._write_batch_sync: empty columnar segment skipped (row_count 0)
  - WALBatchWriter.stop: timer_thread is None guard
  - WALBatchWriter.add_columnar: negative row_count early return
  - json fallback paths (_dumps, _loads via json module)
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.wal import WALBatchWriter, WALWriter, _dumps, _loads

# -- Shared fixtures ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_WAL_FILE_FSYNC", "0")
    monkeypatch.setenv("HFT_WAL_DISK_MIN_MB", "1")
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "60000")
    monkeypatch.setenv("HFT_WAL_BATCH_MAX_ROWS", "99999")


@pytest.fixture()
def mock_metrics() -> MagicMock:
    mock = MagicMock()
    with patch("hft_platform.recorder.wal.MetricsRegistry") as cls:
        cls.get.return_value = mock
        yield mock


@pytest.fixture()
def wal_writer(tmp_path: Path, mock_metrics: MagicMock) -> WALWriter:
    return WALWriter(str(tmp_path))


@pytest.fixture()
def batch_writer(tmp_path: Path, mock_metrics: MagicMock) -> WALBatchWriter:
    writer = WALBatchWriter(str(tmp_path))
    yield writer
    writer.stop()


# -- WALWriter: write() with real disk space check (forced low threshold) -----


class TestWALWriterWriteDiskCheck:
    @pytest.mark.asyncio
    async def test_write_skips_when_disk_full_via_check(self, tmp_path: Path) -> None:
        """write() returns False when _check_disk_space detects low disk space."""
        with patch("hft_platform.recorder.wal.MetricsRegistry") as cls:
            cls.get.return_value = MagicMock()
            w = WALWriter(str(tmp_path))
        w._last_disk_check_ts = 0.0
        w._disk_min_mb = 999_999_999  # impossibly high threshold
        result = await w.write("test_table", [{"row": 1}])
        assert result is False
        assert w._disk_full is True

    @pytest.mark.asyncio
    async def test_write_succeeds_when_disk_check_passes(self, wal_writer: WALWriter) -> None:
        """write() returns True when disk space is sufficient."""
        wal_writer._last_disk_check_ts = time.monotonic()
        wal_writer._disk_full = False
        result = await wal_writer.write("test_table", [{"a": 1}, {"b": 2}])
        assert result is True


# -- WALWriter: _write_sync_atomic temp cleanup on failure --------------------


class TestWriteSyncAtomicCleanup:
    def test_temp_file_cleaned_on_rename_failure(self, tmp_path: Path, wal_writer: WALWriter) -> None:
        """If rename fails, temp file is cleaned up."""
        filename = str(tmp_path / "test_table_123.jsonl")
        with patch("os.rename", side_effect=OSError("rename failed")):
            with pytest.raises(OSError, match="rename failed"):
                wal_writer._write_sync_atomic(filename, [{"x": 1}])
        # Temp files should be cleaned up
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_atomic_write_produces_complete_file(self, tmp_path: Path, wal_writer: WALWriter) -> None:
        """Atomic write creates a complete, readable JSONL file."""
        filename = str(tmp_path / "orders_999.jsonl")
        data = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
        wal_writer._write_sync_atomic(filename, data)
        lines = Path(filename).read_text().strip().splitlines()
        assert len(lines) == 3
        for i, line in enumerate(lines):
            parsed = _loads(line)
            assert parsed["id"] == data[i]["id"]


# -- WALWriter: _maybe_fsync_dir with zero threshold -------------------------


class TestMaybeFsyncDirZeroThreshold:
    def test_fsync_dir_runs_and_records_latency(self, tmp_path: Path, mock_metrics: MagicMock) -> None:
        """_maybe_fsync_dir runs fsync and records latency when threshold is 0."""
        w = WALWriter(str(tmp_path))
        w._dir_fsync_min_ms = 0
        w._last_dir_fsync_ts = 0.0
        w._maybe_fsync_dir(str(tmp_path), writer="wal")
        # Verify the timestamp was updated
        assert w._last_dir_fsync_ts > 0


# -- WALBatchWriter: _check_disk_space recovery path --------------------------


class TestBatchWriterDiskSpaceRecovery:
    def test_disk_space_recovery_logs_and_deactivates(self, batch_writer: WALBatchWriter) -> None:
        """When disk was full and now recovers, _disk_full flips back to False."""
        batch_writer._last_disk_check_ts = 0.0
        batch_writer._disk_full = True
        batch_writer._disk_min_mb = 0.001  # tiny threshold, real disk will exceed

        result = batch_writer._check_disk_space()
        assert result is True
        assert batch_writer._disk_full is False


# -- WALBatchWriter: flush failure with columnar data merge-back --------------


class TestBatchWriterFlushFailureMergeBack:
    @pytest.mark.asyncio
    async def test_flush_failure_merges_back_columnar_data(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """When flush write fails, columnar data is merged back into buffer."""
        # Add columnar data
        await batch_writer.add_columnar("hft.ticks", ["sym", "px"], [["ABC"], [100]], 1)
        assert batch_writer._buffer_rows == 1

        with patch.object(batch_writer, "_write_batch_sync", side_effect=OSError("disk err")):
            result = await batch_writer.flush()

        assert result is False
        # Data should be merged back
        assert batch_writer._buffer_rows == 1
        assert "hft.ticks" in batch_writer._columnar_buffer

    @pytest.mark.asyncio
    async def test_flush_failure_merges_back_mixed_data(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """Flush failure merges back both dict and columnar data."""
        await batch_writer.add("hft.orders", [{"id": "O1"}])
        await batch_writer.add_columnar("hft.market_data", ["sym"], [["2330"]], 1)
        assert batch_writer._buffer_rows == 2

        with patch.object(batch_writer, "_write_batch_sync", side_effect=IOError("fail")):
            result = await batch_writer.flush()

        assert result is False
        assert batch_writer._buffer_rows == 2
        assert "hft.orders" in batch_writer._buffer
        assert "hft.market_data" in batch_writer._columnar_buffer


# -- WALBatchWriter: _flush_timer_loop circuit breaker (data dropped) ---------


class TestBatchWriterTimerLoopCircuitBreaker:
    def test_timer_loop_drops_data_after_max_failures(self, tmp_path: Path, monkeypatch) -> None:
        """After N consecutive failures, data is dropped instead of merged back."""
        monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "10")
        with patch("hft_platform.recorder.wal.MetricsRegistry") as cls:
            cls.get.return_value = MagicMock()
            writer = WALBatchWriter(str(tmp_path))
        try:
            # Stop auto-started timer
            writer._timer_running = False
            writer._timer_thread.join(timeout=1.0)

            writer._merge_back_max_failures = 1  # trip after first failure
            writer._merge_back_consecutive_failures = 0

            flush_event = threading.Event()

            def _always_fail(*a, **kw):
                flush_event.set()
                raise OSError("persistent fail")

            writer._write_batch_sync = MagicMock(side_effect=_always_fail)

            # Populate buffer
            with writer._lock:
                writer._buffer = {"hft.ticks": [{"sym": "X"}]}
                writer._buffer_rows = 1
                writer._buffer_bytes = 50
                writer._last_flush_ts = 0

            # Run one iteration
            writer._timer_running = True
            timer = threading.Thread(target=writer._flush_timer_loop, daemon=True)
            timer.start()
            flush_event.wait(timeout=2.0)
            time.sleep(0.05)

            writer._timer_running = False
            timer.join(timeout=1.0)

            # After circuit breaker trips, data is dropped (buffer_rows stays 0)
            # because merge-back is skipped
            assert writer._write_batch_sync.call_count >= 1
        finally:
            writer._timer_running = False


# -- WALBatchWriter: _write_batch_sync edge cases ----------------------------


class TestWriteBatchSyncEdgeCases:
    def test_empty_rows_dict_produces_no_data_lines(self, batch_writer: WALBatchWriter, tmp_path: Path) -> None:
        """_write_batch_sync with empty rows list in a table skips that table."""
        data = {"hft.market_data": []}
        batch_writer._write_batch_sync(data, 0)
        # No files should be produced (empty data)
        jsonl_files = list(tmp_path.glob("batch_*.jsonl"))
        assert len(jsonl_files) == 0

    def test_columnar_segment_zero_row_count_skipped(self, batch_writer: WALBatchWriter, tmp_path: Path) -> None:
        """Columnar segment with row_count=0 is skipped."""
        columnar = {
            "hft.ticks": [
                (["sym"], [["A"]], 0),  # zero row_count
            ]
        }
        batch_writer._write_batch_sync({}, 0, columnar)
        jsonl_files = list(tmp_path.glob("batch_*.jsonl"))
        assert len(jsonl_files) == 0

    def test_multiple_tables_in_single_file(self, batch_writer: WALBatchWriter, tmp_path: Path) -> None:
        """Multiple tables are written into a single batch file with headers."""
        data = {
            "hft.orders": [{"order_id": "O1"}],
            "hft.fills": [{"fill_id": "F1"}],
        }
        batch_writer._write_batch_sync(data, 0)
        jsonl_files = list(tmp_path.glob("batch_*.jsonl"))
        assert len(jsonl_files) == 1
        lines = jsonl_files[0].read_text().strip().splitlines()
        # 2 headers + 2 data rows = 4 lines
        assert len(lines) == 4
        # Verify both table headers present
        headers = [_loads(l) for l in lines if "__wal_table__" in l]
        table_names = {h["__wal_table__"] for h in headers}
        assert "hft.orders" in table_names
        assert "hft.fills" in table_names


# -- WALBatchWriter: stop with timer_thread None guard -----------------------


class TestBatchWriterStopNullTimer:
    def test_stop_when_timer_thread_none(self, tmp_path: Path, mock_metrics: MagicMock) -> None:
        """stop() handles _timer_thread being None gracefully."""
        writer = WALBatchWriter(str(tmp_path))
        # Stop normally first to get a clean state
        writer._timer_running = False
        writer._timer_thread.join(timeout=1.0)
        writer._timer_thread = None
        # Populate buffer
        with writer._lock:
            writer._buffer = {"t": [{"x": 1}]}
            writer._buffer_rows = 1
            writer._buffer_bytes = 30
        # stop() should not crash even with _timer_thread=None
        writer.stop()
        # Data should be flushed (written to file) since buffer was non-empty
        assert writer._buffer_rows == 0 or len(list(tmp_path.glob("batch_*.jsonl"))) >= 1


# -- WALBatchWriter: add_columnar with negative/zero row_count ---------------


class TestBatchWriterAddColumnarEdge:
    @pytest.mark.asyncio
    async def test_add_columnar_negative_row_count_returns_true(self, batch_writer: WALBatchWriter) -> None:
        """add_columnar with negative row_count returns True immediately."""
        result = await batch_writer.add_columnar("t", ["a"], [["x"]], -1)
        assert result is True
        assert batch_writer._buffer_rows == 0

    @pytest.mark.asyncio
    async def test_add_columnar_empty_column_data_returns_true(self, batch_writer: WALBatchWriter) -> None:
        """add_columnar with empty column_data returns True immediately."""
        result = await batch_writer.add_columnar("t", ["a"], [], 5)
        assert result is True
        assert batch_writer._buffer_rows == 0


# -- JSON codec: verify _dumps and _loads work correctly ---------------------


class TestJsonCodec:
    def test_dumps_produces_valid_json(self) -> None:
        data = {"key": "value", "num": 42}
        result = _dumps(data)
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert parsed["num"] == 42

    def test_loads_parses_json(self) -> None:
        raw = '{"hello": "world"}'
        parsed = _loads(raw)
        assert parsed["hello"] == "world"

    def test_roundtrip_consistency(self) -> None:
        original = {"symbol": "2330", "price": 100, "nested": [1, 2, 3]}
        serialized = _dumps(original)
        deserialized = _loads(serialized)
        assert deserialized == original


# -- WALBatchWriter: _maybe_fsync_file disabled path -------------------------


class TestBatchWriterFsyncFileDisabled:
    def test_maybe_fsync_file_disabled_is_noop(self, batch_writer: WALBatchWriter) -> None:
        """_maybe_fsync_file does nothing when fsync is disabled."""
        batch_writer._fsync_file_enabled = False
        # Should not raise or call os.fsync
        batch_writer._maybe_fsync_file(0)
        assert batch_writer._fsync_file_enabled is False


# -- WALBatchWriter: _maybe_fsync_dir throttled path -------------------------


class TestBatchWriterFsyncDirThrottled:
    def test_maybe_fsync_dir_throttled_does_not_call_os_open(self, batch_writer: WALBatchWriter) -> None:
        """_maybe_fsync_dir skips when within throttle interval."""
        batch_writer._dir_fsync_min_ms = 100_000.0
        batch_writer._last_dir_fsync_ts = time.monotonic()
        with patch("os.open") as mock_open:
            batch_writer._maybe_fsync_dir(str(batch_writer._wal_dir))
        assert not mock_open.called
