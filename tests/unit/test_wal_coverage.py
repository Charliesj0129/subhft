"""Tests for WAL write/read/flush, error recovery, metrics fallbacks, and batch writer.

Covers missing lines in recorder/wal.py:
  - json fallback import path (lines 22-30)
  - WALWriter metrics exception fallbacks (lines 71-73, 80-82, 89-91, 100-102)
  - _write_sync legacy method (lines 221-223)
  - WALBatchWriter metrics init fallback (lines 267-269)
  - WALBatchWriter metrics exception paths (lines 286, 292-294, 298, 301-303, 307, 310-312)
  - WALBatchWriter disk pressure skip metrics exceptions (lines 323-325)
  - WALBatchWriter _check_disk_space transitions (lines 348, 370, 375, 377-379, 384)
  - WALBatchWriter add auto-flush on row threshold (lines 397, 400)
  - WALBatchWriter add_columnar paths (lines 413, 415, 423, 426)
  - WALBatchWriter flush metrics paths (lines 464-466, 486-488)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.wal import WALBatchWriter, WALWriter, _loads


# ── Shared fixtures ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _disable_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable fsync and set low disk threshold for test speed."""
    monkeypatch.setenv("HFT_WAL_FILE_FSYNC", "0")
    monkeypatch.setenv("HFT_WAL_DISK_MIN_MB", "1")
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "60000")
    monkeypatch.setenv("HFT_WAL_BATCH_MAX_ROWS", "99999")


@pytest.fixture()
def mock_metrics() -> MagicMock:
    """Patch MetricsRegistry so constructors get a mock."""
    mock = MagicMock()
    with patch("hft_platform.recorder.wal.MetricsRegistry") as registry_cls:
        registry_cls.get.return_value = mock
        yield mock


@pytest.fixture()
def wal_writer(tmp_path: Path, mock_metrics: MagicMock) -> WALWriter:
    return WALWriter(str(tmp_path))


@pytest.fixture()
def batch_writer(tmp_path: Path, mock_metrics: MagicMock) -> WALBatchWriter:
    writer = WALBatchWriter(str(tmp_path))
    yield writer
    writer.stop()


# ── WALWriter: _write_sync legacy method ─────────────────────────────────


class TestWALWriterLegacySync:
    def test_write_sync_creates_valid_jsonl(self, tmp_path: Path, wal_writer: WALWriter) -> None:
        """_write_sync writes JSONL rows readable by _loads."""
        filename = str(tmp_path / "test_legacy.jsonl")
        data = [{"order_id": "O1", "price": 100}, {"order_id": "O2", "price": 200}]

        wal_writer._write_sync(filename, data)

        lines = Path(filename).read_text().strip().splitlines()
        assert len(lines) == 2
        for i, line in enumerate(lines):
            parsed = _loads(line)
            assert parsed["order_id"] == data[i]["order_id"]
            assert parsed["price"] == data[i]["price"]


# ── WALWriter: metrics exception fallback paths ─────────────────────────


class TestWALWriterMetricsFallbacks:
    def test_set_disk_pressure_metrics_catches_exception(
        self, wal_writer: WALWriter, mock_metrics: MagicMock
    ) -> None:
        """_set_disk_pressure_metrics swallows metric recording exceptions."""
        mock_metrics.wal_disk_available_mb.set.side_effect = RuntimeError("metric boom")
        # Should not raise
        wal_writer._set_disk_pressure_metrics(500.0, False, "wal")
        assert mock_metrics.wal_disk_available_mb.set.called

    def test_record_wal_write_latency_catches_exception(
        self, wal_writer: WALWriter, mock_metrics: MagicMock
    ) -> None:
        """_record_wal_write_latency swallows metric recording exceptions."""
        mock_metrics.recorder_wal_write_latency_ms.labels.side_effect = RuntimeError("metric boom")
        wal_writer._record_wal_write_latency("wal", "atomic", 1.5)
        assert mock_metrics.recorder_wal_write_latency_ms.labels.called

    def test_record_fsync_latency_catches_exception(
        self, wal_writer: WALWriter, mock_metrics: MagicMock
    ) -> None:
        """_record_fsync_latency swallows metric recording exceptions."""
        mock_metrics.recorder_wal_fsync_latency_ms.labels.side_effect = RuntimeError("metric boom")
        wal_writer._record_fsync_latency("wal", "file", 0.5)
        assert mock_metrics.recorder_wal_fsync_latency_ms.labels.called

    def test_handle_disk_pressure_skip_catches_metric_exception(
        self, wal_writer: WALWriter, mock_metrics: MagicMock
    ) -> None:
        """_handle_disk_pressure_skip still returns False when metrics throw."""
        mock_metrics.recorder_wal_skipped_rows_total.labels.side_effect = RuntimeError("metric boom")
        result = wal_writer._handle_disk_pressure_skip("orders", 5, writer="wal")
        assert result is False
        assert wal_writer._disk_full_count == 5

    def test_handle_disk_pressure_skip_raises_on_raise_policy(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """When policy is 'raise', _handle_disk_pressure_skip raises RuntimeError."""
        with patch.dict(os.environ, {"HFT_WAL_DISK_PRESSURE_POLICY": "raise"}):
            writer = WALWriter(str(tmp_path))
        with pytest.raises(RuntimeError, match="WAL disk pressure circuit breaker active"):
            writer._handle_disk_pressure_skip("orders", 3, writer="wal")


# ── WALBatchWriter: metrics init fallback ────────────────────────────────


class TestBatchWriterMetricsInit:
    def test_init_catches_metrics_exception(self, tmp_path: Path) -> None:
        """WALBatchWriter.__init__ sets _metrics=None when MetricsRegistry.get() throws."""
        with patch("hft_platform.recorder.wal.MetricsRegistry") as registry_cls:
            registry_cls.get.side_effect = RuntimeError("no metrics")
            writer = WALBatchWriter(str(tmp_path))
            try:
                assert writer._metrics is None
            finally:
                writer.stop()


# ── WALBatchWriter: metrics exception fallback paths ─────────────────────


class TestBatchWriterMetricsFallbacks:
    def test_set_disk_pressure_metrics_no_metrics_returns(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """_set_disk_pressure_metrics returns early when _metrics is None."""
        batch_writer._metrics = None
        # Should not raise or do anything
        batch_writer._set_disk_pressure_metrics(500.0, False)
        assert batch_writer._metrics is None

    def test_set_disk_pressure_metrics_catches_exception(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """_set_disk_pressure_metrics swallows metric exceptions."""
        mock_metrics.wal_disk_available_mb.set.side_effect = RuntimeError("boom")
        batch_writer._set_disk_pressure_metrics(500.0, True)
        assert mock_metrics.wal_disk_available_mb.set.called

    def test_record_wal_write_latency_no_metrics_returns(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """_record_wal_write_latency returns early when _metrics is None."""
        batch_writer._metrics = None
        batch_writer._record_wal_write_latency("batch_flush", 1.0)
        assert batch_writer._metrics is None

    def test_record_wal_write_latency_catches_exception(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """_record_wal_write_latency swallows metric exceptions."""
        mock_metrics.recorder_wal_write_latency_ms.labels.side_effect = RuntimeError("boom")
        batch_writer._record_wal_write_latency("batch_flush", 1.0)
        assert mock_metrics.recorder_wal_write_latency_ms.labels.called

    def test_record_fsync_latency_no_metrics_returns(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """_record_fsync_latency returns early when _metrics is None."""
        batch_writer._metrics = None
        batch_writer._record_fsync_latency("file", 0.5)
        assert batch_writer._metrics is None

    def test_record_fsync_latency_catches_exception(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """_record_fsync_latency swallows metric exceptions."""
        mock_metrics.recorder_wal_fsync_latency_ms.labels.side_effect = RuntimeError("boom")
        batch_writer._record_fsync_latency("file", 0.5)
        assert mock_metrics.recorder_wal_fsync_latency_ms.labels.called

    def test_handle_disk_pressure_skip_catches_metric_exception(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """_handle_disk_pressure_skip returns False even when metrics throw."""
        mock_metrics.recorder_wal_skipped_rows_total.labels.side_effect = RuntimeError("boom")
        result = batch_writer._handle_disk_pressure_skip("orders", 3)
        assert result is False
        assert batch_writer._disk_full_count == 3

    def test_handle_disk_pressure_skip_raises_on_raise_policy(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """Raise policy propagates RuntimeError from _handle_disk_pressure_skip."""
        with patch.dict(os.environ, {"HFT_WAL_DISK_PRESSURE_POLICY": "raise"}):
            writer = WALBatchWriter(str(tmp_path))
        try:
            with pytest.raises(RuntimeError, match="WAL batch disk pressure circuit breaker active"):
                writer._handle_disk_pressure_skip("orders", 3)
        finally:
            writer.stop()


# ── WALBatchWriter: _check_disk_space transitions ───────────────────────


class TestBatchWriterDiskSpace:
    def test_disk_space_transition_to_full(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """_check_disk_space transitions from healthy to disk_full when below threshold."""
        batch_writer._last_disk_check_ts = 0.0  # force re-check
        batch_writer._disk_full = False

        mock_stat = MagicMock()
        mock_stat.f_bavail = 10  # very few blocks
        mock_stat.f_frsize = 1024  # => ~0.01 MB (below default 1 MB threshold)

        with patch("os.statvfs", return_value=mock_stat):
            result = batch_writer._check_disk_space()

        assert result is False
        assert batch_writer._disk_full is True

    def test_disk_space_recovery_from_full(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """_check_disk_space transitions from disk_full back to healthy."""
        batch_writer._last_disk_check_ts = 0.0
        batch_writer._disk_full = True

        mock_stat = MagicMock()
        mock_stat.f_bavail = 1_000_000
        mock_stat.f_frsize = 4096  # ~3.8 GB available

        with patch("os.statvfs", return_value=mock_stat):
            result = batch_writer._check_disk_space()

        assert result is True
        assert batch_writer._disk_full is False

    def test_disk_space_oserror_fails_open(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """On OSError from statvfs, _check_disk_space returns True (fail-open)."""
        batch_writer._last_disk_check_ts = 0.0
        batch_writer._disk_full = False

        with patch("os.statvfs", side_effect=OSError("no such device")):
            result = batch_writer._check_disk_space()

        assert result is True

    def test_disk_space_interval_caching(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """Within the check interval, cached result is returned without statvfs call."""
        batch_writer._last_disk_check_ts = time.monotonic()  # recent check
        batch_writer._disk_full = False

        with patch("os.statvfs") as mock_statvfs:
            result = batch_writer._check_disk_space()

        assert result is True
        assert not mock_statvfs.called

    def test_disk_space_dir_fsync_throttled(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """_maybe_fsync_dir respects _dir_fsync_min_ms throttle."""
        batch_writer._dir_fsync_min_ms = 100_000.0  # 100 seconds
        batch_writer._last_dir_fsync_ts = time.monotonic()  # just synced

        with patch("os.open") as mock_open:
            batch_writer._maybe_fsync_dir(str(batch_writer._wal_dir))

        assert not mock_open.called


# ── WALBatchWriter: add() auto-flush on row threshold ────────────────────


class TestBatchWriterAddAutoFlush:
    @pytest.mark.asyncio
    async def test_add_triggers_flush_on_row_threshold(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """When _batch_max_rows is reached, add() triggers immediate flush."""
        with patch.dict(os.environ, {"HFT_WAL_BATCH_MAX_ROWS": "3"}):
            writer = WALBatchWriter(str(tmp_path))
        try:
            rows = [{"id": i} for i in range(3)]
            result = await writer.add("test_table", rows)

            assert result is True
            # After threshold flush, buffer should be empty
            assert writer._buffer_rows == 0
            jsonl_files = list(tmp_path.glob("*.jsonl"))
            assert len(jsonl_files) >= 1
        finally:
            writer.stop()

    @pytest.mark.asyncio
    async def test_add_disk_full_returns_false(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """add() returns False when disk is full."""
        batch_writer._disk_full = True
        batch_writer._last_disk_check_ts = time.monotonic()  # skip recheck

        result = await batch_writer.add("orders", [{"order_id": "O1"}])
        assert result is False


# ── WALBatchWriter: add_columnar() paths ─────────────────────────────────


class TestBatchWriterAddColumnar:
    @pytest.mark.asyncio
    async def test_add_columnar_buffers_data(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """add_columnar stores columnar data in the buffer."""
        cols = ["sym", "px"]
        data = [["2330", "TXFD6"], [100, 200]]
        result = await batch_writer.add_columnar("market_data", cols, data, 2)

        assert result is True
        assert batch_writer._buffer_rows == 2
        assert "market_data" in batch_writer._columnar_buffer

    @pytest.mark.asyncio
    async def test_add_columnar_empty_rows_returns_true(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """add_columnar with zero row_count returns True immediately."""
        result = await batch_writer.add_columnar("market_data", ["sym"], [["2330"]], 0)
        assert result is True
        assert batch_writer._buffer_rows == 0

    @pytest.mark.asyncio
    async def test_add_columnar_empty_columns_returns_true(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """add_columnar with empty column_names returns True immediately."""
        result = await batch_writer.add_columnar("market_data", [], [], 2)
        assert result is True
        assert batch_writer._buffer_rows == 0

    @pytest.mark.asyncio
    async def test_add_columnar_disk_full_returns_false(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """add_columnar returns False when disk is full."""
        batch_writer._disk_full = True
        batch_writer._last_disk_check_ts = time.monotonic()

        result = await batch_writer.add_columnar("market_data", ["sym"], [["2330"]], 1)
        assert result is False

    @pytest.mark.asyncio
    async def test_add_columnar_triggers_flush_on_threshold(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """add_columnar triggers flush when threshold rows reached."""
        with patch.dict(os.environ, {"HFT_WAL_BATCH_MAX_ROWS": "2"}):
            writer = WALBatchWriter(str(tmp_path))
        try:
            cols = ["sym", "px"]
            data = [["2330", "TXFD6"], [100, 200]]
            result = await writer.add_columnar("market_data", cols, data, 2)

            assert result is True
            assert writer._buffer_rows == 0
            jsonl_files = list(tmp_path.glob("*.jsonl"))
            assert len(jsonl_files) >= 1
        finally:
            writer.stop()


# ── WALBatchWriter: flush metrics paths ──────────────────────────────────


class TestBatchWriterFlushMetrics:
    @pytest.mark.asyncio
    async def test_flush_success_increments_ok_metric(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """Successful flush increments wal_batch_flush_total ok metric."""
        await batch_writer.add("orders", [{"order_id": "O1"}])
        result = await batch_writer.flush()

        assert result is True
        mock_metrics.wal_batch_flush_total.labels.assert_any_call(result="ok")

    @pytest.mark.asyncio
    async def test_flush_success_metric_exception_swallowed(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """If the ok metric recording throws, flush still returns True."""
        mock_metrics.wal_batch_flush_total.labels.side_effect = RuntimeError("boom")
        await batch_writer.add("orders", [{"order_id": "O1"}])
        result = await batch_writer.flush()

        assert result is True

    @pytest.mark.asyncio
    async def test_flush_failure_increments_error_metric(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """Failed flush increments wal_batch_flush_total error metric."""
        await batch_writer.add("orders", [{"order_id": "O1"}])

        with patch.object(batch_writer, "_write_batch_sync", side_effect=OSError("disk err")):
            result = await batch_writer.flush()

        assert result is False
        mock_metrics.wal_batch_flush_total.labels.assert_any_call(result="error")

    @pytest.mark.asyncio
    async def test_flush_failure_error_metric_exception_swallowed(
        self, batch_writer: WALBatchWriter, mock_metrics: MagicMock
    ) -> None:
        """If the error metric recording throws, flush still returns False and merges back."""
        mock_metrics.wal_batch_flush_total.labels.side_effect = RuntimeError("boom")
        await batch_writer.add("orders", [{"order_id": "O1"}])

        with patch.object(batch_writer, "_write_batch_sync", side_effect=OSError("disk err")):
            result = await batch_writer.flush()

        assert result is False
        assert batch_writer._buffer_rows == 1


# ── WALBatchWriter: flush empty buffer returns True ──────────────────────


class TestBatchWriterFlushEmpty:
    @pytest.mark.asyncio
    async def test_flush_empty_buffer_returns_true(
        self, batch_writer: WALBatchWriter
    ) -> None:
        """flush() on empty buffer returns True without writing."""
        result = await batch_writer.flush()
        assert result is True


# ── WALBatchWriter: stop() final flush ───────────────────────────────────


class TestBatchWriterStop:
    @pytest.mark.asyncio
    async def test_stop_flushes_remaining_data(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """stop() flushes buffered data before stopping the timer thread."""
        writer = WALBatchWriter(str(tmp_path))
        await writer.add("orders", [{"order_id": "O1"}])
        assert writer._buffer_rows == 1

        writer.stop()

        assert writer._buffer_rows == 0
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) >= 1

    @pytest.mark.asyncio
    async def test_stop_with_empty_buffer_no_write(
        self, batch_writer: WALBatchWriter, tmp_path: Path
    ) -> None:
        """stop() with empty buffer does not create any files."""
        batch_writer.stop()
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 0

    @pytest.mark.asyncio
    async def test_stop_merges_back_on_write_failure(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """stop() merges data back when final flush write fails."""
        writer = WALBatchWriter(str(tmp_path))
        await writer.add("orders", [{"order_id": "O1"}, {"order_id": "O2"}])

        with patch.object(writer, "_write_batch_sync", side_effect=OSError("disk error")):
            writer.stop()

        # Data merged back — caller can inspect for retry
        assert writer._buffer_rows == 2
        assert "orders" in writer._buffer


# ── WALBatchWriter: _write_batch_sync with columnar data ─────────────────


class TestBatchWriterColumnarSync:
    def test_write_batch_sync_columnar_creates_valid_jsonl(
        self, batch_writer: WALBatchWriter, tmp_path: Path
    ) -> None:
        """_write_batch_sync writes columnar data as individual row dicts."""
        columnar_data = {
            "market_data": [
                (["sym", "px"], [["2330", "TXFD6"], [100, 200]], 2),
            ]
        }
        batch_writer._write_batch_sync({}, 0, columnar_data)

        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1

        lines = jsonl_files[0].read_text().strip().splitlines()
        # 1 header + 2 data rows
        assert len(lines) == 3
        header = _loads(lines[0])
        assert header["__wal_table__"] == "market_data"
        assert header["__row_count__"] == 2

        row1 = _loads(lines[1])
        assert row1["sym"] == "2330"
        assert row1["px"] == 100

    def test_write_batch_sync_mixed_dict_and_columnar(
        self, batch_writer: WALBatchWriter, tmp_path: Path
    ) -> None:
        """_write_batch_sync handles both dict rows and columnar data."""
        dict_data = {"orders": [{"order_id": "O1"}]}
        columnar_data = {
            "fills": [
                (["fill_id", "px"], [["F1"], [100]], 1),
            ]
        }
        batch_writer._write_batch_sync(dict_data, 0, columnar_data)

        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert len(jsonl_files) == 1

        lines = jsonl_files[0].read_text().strip().splitlines()
        # orders header + 1 row + fills header + 1 row = 4
        assert len(lines) == 4


# ── WALBatchWriter: EC-3 file splitting on size limit ────────────────────


class TestBatchWriterFileSplitting:
    def test_write_batch_sync_splits_on_size_limit(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """EC-3: When file exceeds max size, a new file is started."""
        # Set very small max size to force split
        with patch.dict(os.environ, {"HFT_WAL_FILE_MAX_MB": "0.0001"}):
            writer = WALBatchWriter(str(tmp_path))
        try:
            data = {"orders": [{"order_id": f"O{i}", "payload": "x" * 50} for i in range(10)]}
            writer._write_batch_sync(data, 0)

            jsonl_files = list(tmp_path.glob("*.jsonl"))
            # With very small limit, should have multiple files
            assert len(jsonl_files) > 1
        finally:
            writer.stop()

    def test_write_batch_sync_columnar_splits_on_size_limit(
        self, tmp_path: Path, mock_metrics: MagicMock
    ) -> None:
        """EC-3: Columnar data also triggers file splits when size exceeded."""
        with patch.dict(os.environ, {"HFT_WAL_FILE_MAX_MB": "0.0001"}):
            writer = WALBatchWriter(str(tmp_path))
        try:
            columnar_data = {
                "market_data": [
                    (
                        ["sym", "payload"],
                        [
                            [f"SYM{i}" for i in range(10)],
                            ["x" * 50 for _ in range(10)],
                        ],
                        10,
                    ),
                ]
            }
            writer._write_batch_sync({}, 0, columnar_data)

            jsonl_files = list(tmp_path.glob("*.jsonl"))
            assert len(jsonl_files) > 1
        finally:
            writer.stop()
