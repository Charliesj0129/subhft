import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.recorder.wal import WALBatchWriter, WALWriter


def test_wal_write_sync_atomic_creates_file(tmp_path: Path):
    writer = WALWriter(str(tmp_path))
    fname = tmp_path / "hft.market_data_123.jsonl"
    payload = [{"symbol": "TEST", "price": 123, "volume": 1}]

    writer._write_sync_atomic(str(fname), payload)

    assert fname.exists()
    lines = fname.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == payload[0]


def test_wal_writer_disk_pressure_raise_policy(tmp_path: Path):
    """_handle_disk_pressure_skip raises RuntimeError when policy is 'raise'."""
    writer = WALWriter(str(tmp_path))
    writer._disk_full = True
    writer._disk_pressure_policy = "raise"

    with pytest.raises(RuntimeError, match="circuit breaker"):
        writer._handle_disk_pressure_skip("hft.market_data", 10, writer="wal")


def test_wal_writer_disk_pressure_halt_policy_returns_false(tmp_path: Path):
    """_handle_disk_pressure_skip returns False (no raise) when policy is 'halt'."""
    writer = WALWriter(str(tmp_path))
    writer._disk_full = True
    writer._disk_pressure_policy = "halt"

    result = writer._handle_disk_pressure_skip("hft.market_data", 5, writer="wal")
    assert result is False


def test_wal_batch_writer_disk_pressure_raise_policy(tmp_path: Path, monkeypatch):
    """WALBatchWriter._handle_disk_pressure_skip raises RuntimeError when policy is 'raise'."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    monkeypatch.setenv("HFT_WAL_DISK_PRESSURE_POLICY", "raise")
    writer = WALBatchWriter(str(tmp_path))
    try:
        writer._disk_full = True
        with pytest.raises(RuntimeError, match="circuit breaker"):
            writer._handle_disk_pressure_skip("hft.market_data", 3)
    finally:
        writer.stop()


def test_wal_batch_writer_disk_pressure_halt_returns_false(tmp_path: Path, monkeypatch):
    """WALBatchWriter._handle_disk_pressure_skip returns False when policy is 'halt'."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    monkeypatch.setenv("HFT_WAL_DISK_PRESSURE_POLICY", "halt")
    writer = WALBatchWriter(str(tmp_path))
    try:
        writer._disk_full = True
        result = writer._handle_disk_pressure_skip("hft.market_data", 3)
        assert result is False
    finally:
        writer.stop()


@pytest.mark.asyncio
async def test_wal_batch_writer_flush_empty_buffer_no_op(tmp_path: Path, monkeypatch):
    """flush() returns True immediately when buffer is empty without writing any file."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        result = await writer.flush()
        assert result is True
        assert list(tmp_path.glob("*.jsonl")) == []
    finally:
        writer.stop()


@pytest.mark.asyncio
async def test_wal_batch_writer_stop_flushes_buffered_rows(tmp_path: Path, monkeypatch):
    """stop() writes buffered data to disk without needing an explicit flush()."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    ok = await writer.add_columnar(
        "hft.ticks",
        ["symbol", "price"],
        [["ABC"], [12345]],
        1,
    )
    assert ok is True
    writer.stop()

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) >= 1, "stop() must have written at least one WAL file"


@pytest.mark.asyncio
async def test_wal_batch_writer_ec3_file_splitting(tmp_path: Path, monkeypatch):
    """_write_batch_sync splits into multiple files when data exceeds _file_max_bytes."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        writer._file_max_bytes = 128

        rows = [{"symbol": f"SYM{i:04d}", "price": i * 10000} for i in range(30)]
        data = {"hft.market_data": rows}
        writer._write_batch_sync(data, 0)

        files = list(tmp_path.glob("batch_*.jsonl"))
        assert len(files) > 1, "EC-3: multiple files must have been written"
    finally:
        writer.stop()


def test_wal_batch_writer_timer_flush_retains_data_on_failure(tmp_path: Path, monkeypatch):
    """When _write_batch_sync fails in timer loop, production code merges data back."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "10")
    writer = WALBatchWriter(str(tmp_path))
    try:
        writer._timer_running = False
        writer._timer_thread.join(timeout=1.0)

        writer._merge_back_max_failures = 999
        flush_event = threading.Event()

        def _always_fail(*args, **kwargs):
            flush_event.set()
            raise OSError("disk fail")

        writer._write_batch_sync = MagicMock(side_effect=_always_fail)

        with writer._lock:
            writer._buffer = {"hft.ticks": [{"sym": "A", "price": 100}]}
            writer._buffer_rows = 1
            writer._buffer_bytes = 50
            writer._last_flush_ts = 0

        writer._timer_running = True
        timer_thread = threading.Thread(target=writer._flush_timer_loop, daemon=True)
        timer_thread.start()

        flush_event.wait(timeout=1.0)
        time.sleep(0.05)

        writer._timer_running = False
        timer_thread.join(timeout=1.0)

        assert writer._write_batch_sync.call_count >= 1

        with writer._lock:
            assert writer._buffer_rows >= 1
            assert "hft.ticks" in writer._buffer
            assert writer._buffer["hft.ticks"] == [{"sym": "A", "price": 100}]
    finally:
        writer._timer_running = False


def test_wal_batch_writer_stop_retains_data_on_failure(tmp_path: Path, monkeypatch):
    """When _write_batch_sync fails in stop(), data is merged back into buffer."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        with writer._lock:
            writer._buffer = {
                "hft.orders": [{"id": "O1"}, {"id": "O2"}],
            }
            writer._buffer_rows = 2
            writer._buffer_bytes = 100

        writer._write_batch_sync = lambda *a, **kw: (_ for _ in ()).throw(IOError("write fail"))

        writer.stop()

        with writer._lock:
            assert writer._buffer_rows == 2
            assert writer._buffer["hft.orders"] == [{"id": "O1"}, {"id": "O2"}]
    finally:
        writer._timer_running = False


def test_wal_batch_writer_stop_clears_on_success(tmp_path: Path, monkeypatch):
    """When stop() write succeeds, buffer is properly cleared."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        with writer._lock:
            writer._buffer = {"hft.ticks": [{"sym": "X"}]}
            writer._buffer_rows = 1
            writer._buffer_bytes = 30

        writer.stop()

        with writer._lock:
            assert writer._buffer_rows == 0
            assert writer._buffer == {}
    finally:
        writer._timer_running = False


def test_disk_pressure_level_not_clobbered_by_free_space_writer(tmp_path: Path):
    """WAL writers must not write disk_pressure_level — DiskPressureMonitor owns it.

    Both writers used to set it to 2-or-0 from a free-space check on every WAL
    write, overwriting the monitor's 0-3 WAL-size level. On 2026-07-24 that made
    the gauge read 0.0 while the monitor was CRITICAL, so WALDiskPressureCritical
    flapped and HALT was never visible in Prometheus at all.
    """
    writer = WALWriter(str(tmp_path))
    metrics = MagicMock()
    writer._metrics = metrics

    writer._set_disk_pressure_metrics(123.0, True, "wal_sync")

    metrics.disk_pressure_level.set.assert_not_called()
    metrics.wal_disk_circuit_breaker_active.labels.assert_called_once_with(writer="wal_sync")
    metrics.wal_disk_circuit_breaker_active.labels.return_value.set.assert_called_once_with(1)
    metrics.wal_disk_available_mb.set.assert_called_once_with(123.0)


def test_batch_writer_disk_pressure_level_not_clobbered_by_free_space_writer():
    """Same ownership rule for WALBatchWriter's free-space reporter."""
    fake_self = MagicMock()
    fake_self._metrics = MagicMock()

    WALBatchWriter._set_disk_pressure_metrics(fake_self, 456.0, False)

    fake_self._metrics.disk_pressure_level.set.assert_not_called()
    fake_self._metrics.wal_disk_circuit_breaker_active.labels.assert_called_once_with(writer="wal_batch")
    fake_self._metrics.wal_disk_circuit_breaker_active.labels.return_value.set.assert_called_once_with(0)
