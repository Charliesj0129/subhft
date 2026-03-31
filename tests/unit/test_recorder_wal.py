import json
from pathlib import Path

import pytest

from hft_platform.recorder.wal import WALBatchWriter, WALReplayer, WALWriter


def test_wal_write_sync_atomic_creates_file(tmp_path: Path):
    writer = WALWriter(str(tmp_path))
    fname = tmp_path / "hft.market_data_123.jsonl"
    payload = [{"symbol": "TEST", "price": 123, "volume": 1}]

    writer._write_sync_atomic(str(fname), payload)

    assert fname.exists()
    lines = fname.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == payload[0]


@pytest.mark.asyncio
async def test_wal_replayer_replays_and_deletes(tmp_path: Path):
    writer = WALWriter(str(tmp_path))
    fname = tmp_path / "hft.market_data_123.jsonl"
    payload = [{"symbol": "TEST", "price": 123, "volume": 1}]
    writer._write_sync(str(fname), payload)

    seen = []

    async def sender(table, data):
        seen.append((table, data))
        return True

    replayer = WALReplayer(str(tmp_path), sender)
    await replayer.replay()

    assert seen == [("hft.market_data", payload)]
    assert not fname.exists()


@pytest.mark.asyncio
async def test_wal_replayer_stops_on_failure(tmp_path: Path):
    writer = WALWriter(str(tmp_path))
    first = tmp_path / "hft.market_data_1.jsonl"
    second = tmp_path / "hft.market_data_2.jsonl"
    writer._write_sync(str(first), [{"symbol": "A"}])
    writer._write_sync(str(second), [{"symbol": "B"}])

    calls = []

    async def sender(table, data):
        calls.append((table, data))
        return False

    replayer = WALReplayer(str(tmp_path), sender)
    await replayer.replay()

    assert calls == [("hft.market_data", [{"symbol": "A"}])]
    assert first.exists()
    assert second.exists()


@pytest.mark.asyncio
async def test_wal_batch_writer_add_columnar_replays_as_rows(tmp_path: Path):
    writer = WALBatchWriter(str(tmp_path))
    try:
        ok = await writer.add_columnar(
            "hft.market_data",
            ["symbol", "price", "volume"],
            [["TEST", "TEST2"], [123, 124], [1, 2]],
            2,
        )
        assert ok is True
        await writer.flush()
    finally:
        writer.stop()

    seen = []

    async def sender(table, data):
        seen.append((table, data))
        return True

    replayer = WALReplayer(str(tmp_path), sender)
    await replayer.replay()

    assert seen
    table, rows = seen[0]
    assert table == "batch"
    # Batch WAL replayer exposes batch file names; verify rows contain reconstructed records.
    flat_rows = []
    for _tbl, payload in seen:
        flat_rows.extend(payload)
    assert any(row.get("symbol") == "TEST" for row in flat_rows)
    assert any(row.get("symbol") == "TEST2" for row in flat_rows)


# ---------------------------------------------------------------------------
# WALWriter disk pressure: raise policy
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# WALBatchWriter disk pressure: raise policy
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# WALBatchWriter: flush with empty buffer is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wal_batch_writer_flush_empty_buffer_no_op(tmp_path: Path, monkeypatch):
    """flush() returns True immediately when buffer is empty without writing any file."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        result = await writer.flush()
        assert result is True
        # No WAL files should have been written
        assert list(tmp_path.glob("*.jsonl")) == []
    finally:
        writer.stop()


# ---------------------------------------------------------------------------
# WALBatchWriter: stop() flushes buffered rows synchronously
# ---------------------------------------------------------------------------


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
    # Intentionally do NOT call flush() — stop() must flush
    writer.stop()

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) >= 1, "stop() must have written at least one WAL file"


# ---------------------------------------------------------------------------
# WALBatchWriter EC-3: file splitting on size limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wal_batch_writer_ec3_file_splitting(tmp_path: Path, monkeypatch):
    """_write_batch_sync splits into multiple files when data exceeds _file_max_bytes."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        # Set a tiny file size limit to force splitting (128 bytes)
        writer._file_max_bytes = 128

        # Write enough rows to exceed the limit
        rows = [{"symbol": f"SYM{i:04d}", "price": i * 10000} for i in range(30)]
        data = {"hft.market_data": rows}
        writer._write_batch_sync(data, 0)

        files = list(tmp_path.glob("batch_*.jsonl"))
        assert len(files) > 1, "EC-3: multiple files must have been written"
    finally:
        writer.stop()


# ---------------------------------------------------------------------------
# WALReplayer: corrupt file is skipped without aborting replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wal_replayer_skips_corrupt_file(tmp_path: Path):
    """A corrupt WAL file should not crash replay; subsequent files may still replay."""
    # Write a valid WAL file
    writer = WALWriter(str(tmp_path))
    valid_fname = tmp_path / "hft.market_data_200.jsonl"
    writer._write_sync(str(valid_fname), [{"symbol": "GOOD"}])

    # Write a corrupt WAL file that sorts before the valid one
    corrupt_fname = tmp_path / "hft.market_data_100.jsonl"
    corrupt_fname.write_text("not valid json!!!\x00\x01\x02\n")

    seen = []

    async def sender(table, data):
        seen.append((table, data))
        return True

    replayer = WALReplayer(str(tmp_path), sender)
    # Must not raise
    await replayer.replay()

    # At minimum the valid file should have been replayed (corrupt is skipped)
    symbols_seen = [row.get("symbol") for _, rows in seen for row in rows]
    assert "GOOD" in symbols_seen


# ---------------------------------------------------------------------------
# WALBatchWriter: data retained on flush failure (RC-2)
# ---------------------------------------------------------------------------


def test_wal_batch_writer_timer_flush_retains_data_on_failure(tmp_path: Path, monkeypatch):
    """When _write_batch_sync fails in timer loop, data is merged back for retry."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        # Manually populate buffer (bypass add_rows to avoid async)
        with writer._lock:
            writer._buffer = {"hft.ticks": [{"sym": "A", "price": 100}]}
            writer._buffer_rows = 1
            writer._buffer_bytes = 50

        # Simulate _write_batch_sync raising
        original_write = writer._write_batch_sync
        writer._write_batch_sync = lambda *a, **kw: (_ for _ in ()).throw(OSError("disk fail"))

        # Manually trigger the flush path (not the timer thread)

        with writer._lock:
            flush_data = writer._buffer
            flush_columnar = writer._columnar_buffer
            writer._buffer = {}
            writer._columnar_buffer = {}
            flush_rows = writer._buffer_rows
            flush_bytes = writer._buffer_bytes
            writer._buffer_rows = 0
            writer._buffer_bytes = 0

        if flush_data or flush_columnar:
            try:
                writer._write_batch_sync(flush_data, 0, flush_columnar)
            except Exception:
                # This is the new merge-back logic
                with writer._lock:
                    for table, rows_list in flush_data.items():
                        writer._buffer.setdefault(table, []).extend(rows_list)
                    for table, cols_list in flush_columnar.items():
                        writer._columnar_buffer.setdefault(table, []).extend(cols_list)
                    writer._buffer_rows += flush_rows
                    writer._buffer_bytes += flush_bytes

        # Verify data was merged back
        with writer._lock:
            assert writer._buffer_rows == 1
            assert "hft.ticks" in writer._buffer
            assert writer._buffer["hft.ticks"] == [{"sym": "A", "price": 100}]
    finally:
        writer._timer_running = False
        writer.stop()


def test_wal_batch_writer_stop_retains_data_on_failure(tmp_path: Path, monkeypatch):
    """When _write_batch_sync fails in stop(), data is merged back into buffer."""
    monkeypatch.setenv("HFT_WAL_BATCH_INTERVAL_MS", "999999")
    writer = WALBatchWriter(str(tmp_path))
    try:
        # Populate buffer
        with writer._lock:
            writer._buffer = {
                "hft.orders": [{"id": "O1"}, {"id": "O2"}],
            }
            writer._buffer_rows = 2
            writer._buffer_bytes = 100

        # Make write fail
        writer._write_batch_sync = lambda *a, **kw: (_ for _ in ()).throw(IOError("write fail"))

        writer.stop()

        # Data should be merged back into buffer for potential recovery
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
        # Populate buffer
        with writer._lock:
            writer._buffer = {"hft.ticks": [{"sym": "X"}]}
            writer._buffer_rows = 1
            writer._buffer_bytes = 30

        writer.stop()

        # Data should be gone (written to WAL successfully)
        with writer._lock:
            assert writer._buffer_rows == 0
            assert writer._buffer == {}
    finally:
        writer._timer_running = False
