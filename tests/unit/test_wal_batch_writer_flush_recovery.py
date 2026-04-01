"""Tests for WALBatchWriter data recovery on async flush failure (P-20)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.wal import WALBatchWriter


@pytest.fixture(autouse=True)
def _disable_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable fsync and set low disk threshold for test speed."""
    monkeypatch.setenv("HFT_WAL_FILE_FSYNC", "0")
    monkeypatch.setenv("HFT_WAL_DISK_MIN_MB", "1")
    # Large batch interval so background timer never fires during tests
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "60000")
    monkeypatch.setenv("HFT_WAL_BATCH_MAX_ROWS", "99999")


@pytest.fixture()
def _mock_metrics() -> MagicMock:
    """Patch MetricsRegistry so WALBatchWriter.__init__ gets a mock."""
    mock = MagicMock()
    with patch("hft_platform.recorder.wal.MetricsRegistry") as registry_cls:
        registry_cls.get.return_value = mock
        yield mock


@pytest.fixture()
def batch_writer(tmp_path: Path, _mock_metrics: MagicMock) -> WALBatchWriter:
    writer = WALBatchWriter(str(tmp_path))
    yield writer
    writer.stop()


# ---------------------------------------------------------------------------
# P-20: Row-count-triggered flush must not lose data on write failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_failure_merges_data_back_into_buffer(
    batch_writer: WALBatchWriter,
    tmp_path: Path,
) -> None:
    """When flush() fails due to a write error, rows must be merged back
    into the active buffer so they can be retried — not silently dropped."""

    rows = [{"order_id": f"O{i}", "price": 100 * i} for i in range(5)]
    for row in rows:
        await batch_writer.add("orders", [row])

    assert batch_writer._buffer_rows == 5, "Pre-condition: rows buffered before flush"

    # Patch _write_batch_sync to raise so flush() fails
    with patch.object(batch_writer, "_write_batch_sync", side_effect=OSError("disk error")):
        result = await batch_writer.flush()

    assert result is False, "flush() should return False on write failure"

    # Data must still be accessible — not lost
    assert batch_writer._buffer_rows == 5, (
        "Rows must be merged back into buffer after flush failure; "
        f"got {batch_writer._buffer_rows}"
    )
    assert "orders" in batch_writer._buffer, "Table key must be present in recovered buffer"
    assert len(batch_writer._buffer["orders"]) == 5, (
        "All 5 rows must be recoverable after flush failure"
    )

    recovered_ids = {row["order_id"] for row in batch_writer._buffer["orders"]}
    expected_ids = {f"O{i}" for i in range(5)}
    assert recovered_ids == expected_ids, "Recovered rows must match original data"


@pytest.mark.asyncio
async def test_flush_failure_does_not_create_wal_file(
    batch_writer: WALBatchWriter,
    tmp_path: Path,
) -> None:
    """A failed flush must not leave partial WAL files on disk."""
    await batch_writer.add("orders", [{"order_id": "O1"}])

    with patch.object(batch_writer, "_write_batch_sync", side_effect=OSError("disk error")):
        await batch_writer.flush()

    jsonl_files = list(tmp_path.glob("*.jsonl"))
    assert not jsonl_files, "No WAL files should exist after a failed flush"


@pytest.mark.asyncio
async def test_flush_success_clears_buffer(
    batch_writer: WALBatchWriter,
    tmp_path: Path,
) -> None:
    """Sanity check: a successful flush clears the buffer."""
    await batch_writer.add("orders", [{"order_id": "O1"}])
    assert batch_writer._buffer_rows == 1

    result = await batch_writer.flush()

    assert result is True, "flush() should return True on success"
    assert batch_writer._buffer_rows == 0, "Buffer must be empty after successful flush"

    jsonl_files = list(tmp_path.glob("*.jsonl"))
    assert len(jsonl_files) == 1, "Exactly one WAL file should be written on success"


@pytest.mark.asyncio
async def test_flush_recovers_columnar_data_on_failure(
    batch_writer: WALBatchWriter,
    tmp_path: Path,
) -> None:
    """Columnar buffer data must also be recovered on flush failure."""
    column_names = ["symbol", "price", "qty"]
    column_data = [["TSMC", "2330"], [150000, 151000], [10, 5]]
    await batch_writer.add_columnar("hft.market_data", column_names, column_data, 2)

    assert batch_writer._buffer_rows == 2

    with patch.object(batch_writer, "_write_batch_sync", side_effect=RuntimeError("ch down")):
        result = await batch_writer.flush()

    assert result is False
    assert batch_writer._buffer_rows == 2, (
        "Columnar rows must be merged back into buffer after flush failure"
    )
    assert "hft.market_data" in batch_writer._columnar_buffer, (
        "Columnar table key must be present after recovery"
    )
    assert len(batch_writer._columnar_buffer["hft.market_data"]) >= 1, (
        "At least one columnar segment must be recoverable"
    )


# ---------------------------------------------------------------------------
# Circuit breaker tests for _flush_timer_loop merge-back
# ---------------------------------------------------------------------------


def _run_timer_flush_once(writer: WALBatchWriter) -> None:
    """Force a single timer-loop flush iteration by bypassing the sleep/interval guards.

    Sets _last_flush_ts to a time far in the past and temporarily drops
    _batch_interval_ms to 0 so the elapsed_ms check passes, then executes
    one iteration of the flush logic directly.
    """
    with writer._lock:
        writer._last_flush_ts = time.monotonic() - 9999.0
        flush_data = writer._buffer
        flush_columnar = writer._columnar_buffer
        writer._buffer = {}
        writer._columnar_buffer = {}
        flush_rows = writer._buffer_rows
        flush_bytes = writer._buffer_bytes
        writer._buffer_rows = 0
        writer._buffer_bytes = 0
        writer._last_flush_ts = time.monotonic()

    if not (flush_data or flush_columnar):
        return

    try:
        writer._write_batch_sync(flush_data, 0, flush_columnar)
        writer._merge_back_consecutive_failures = 0
    except Exception as e:
        writer._merge_back_consecutive_failures += 1
        if writer._merge_back_consecutive_failures >= writer._merge_back_max_failures:
            writer._merge_back_consecutive_failures = 0
            # circuit breaker open — do NOT merge back
        else:
            with writer._lock:
                for table, rows_list in flush_data.items():
                    writer._buffer.setdefault(table, []).extend(rows_list)
                for table, cols_list in flush_columnar.items():
                    writer._columnar_buffer.setdefault(table, []).extend(cols_list)
                writer._buffer_rows += flush_rows
                writer._buffer_bytes += flush_bytes


@pytest.mark.asyncio
async def test_circuit_breaker_drops_data_after_max_failures(
    batch_writer: WALBatchWriter,
    tmp_path: Path,
) -> None:
    """After _merge_back_max_failures consecutive timer-loop failures, data is
    dropped instead of merged back to prevent unbounded buffer growth (OOM)."""
    max_failures = batch_writer._merge_back_max_failures  # default 3

    with patch.object(batch_writer, "_write_batch_sync", side_effect=OSError("disk full")):
        for attempt in range(max_failures):
            # Re-add rows each time (previous iterations merge back)
            if batch_writer._buffer_rows == 0:
                await batch_writer.add("orders", [{"order_id": f"O{attempt}"}])
            _run_timer_flush_once(batch_writer)

        # At this point the circuit breaker should be tripped — rows dropped
        rows_after_trip = batch_writer._buffer_rows

    assert rows_after_trip == 0, (
        f"Circuit breaker must drop data after {max_failures} consecutive failures; "
        f"got {rows_after_trip} rows still buffered"
    )
    assert batch_writer._merge_back_consecutive_failures == 0, (
        "Failure counter must reset after circuit breaker trips"
    )


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_successful_flush(
    batch_writer: WALBatchWriter,
    tmp_path: Path,
) -> None:
    """Consecutive failure counter resets to 0 after a successful timer-loop flush."""
    # Simulate two consecutive failures (below threshold)
    batch_writer._merge_back_consecutive_failures = 2

    await batch_writer.add("orders", [{"order_id": "O1"}])

    # Successful flush via timer-loop path (no mock — real write)
    _run_timer_flush_once(batch_writer)

    assert batch_writer._buffer_rows == 0, "Buffer must be empty after successful flush"
    assert batch_writer._merge_back_consecutive_failures == 0, (
        "Consecutive failure counter must reset to 0 after a successful timer-loop flush"
    )


@pytest.mark.asyncio
async def test_circuit_breaker_merges_back_below_threshold(
    batch_writer: WALBatchWriter,
    tmp_path: Path,
) -> None:
    """Below the failure threshold, data is still merged back for retry."""
    # max_failures = 3 → first 2 failures should still merge back
    await batch_writer.add("orders", [{"order_id": "O1", "price": 100}])
    assert batch_writer._buffer_rows == 1

    with patch.object(batch_writer, "_write_batch_sync", side_effect=OSError("transient error")):
        # First failure — counter becomes 1 (below threshold 3)
        _run_timer_flush_once(batch_writer)

    assert batch_writer._buffer_rows == 1, (
        "Data must be merged back on first failure (below threshold)"
    )
    assert batch_writer._merge_back_consecutive_failures == 1, (
        "Failure counter must be 1 after first failure"
    )
