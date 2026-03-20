"""WAL corruption, replay, and disk pressure resilience tests."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.wal import WALWriter, _loads


@pytest.fixture(autouse=True)
def _disable_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable fsync for faster tests."""
    monkeypatch.setenv("HFT_WAL_FILE_FSYNC", "0")
    monkeypatch.setenv("HFT_WAL_DISK_MIN_MB", "1")


@pytest.fixture()
def _mock_metrics() -> MagicMock:
    """Patch MetricsRegistry so WALWriter.__init__ gets a mock."""
    mock = MagicMock()
    with patch("hft_platform.recorder.wal.MetricsRegistry") as registry_cls:
        registry_cls.get.return_value = mock
        yield mock


@pytest.fixture()
def wal_writer(tmp_path: Path, _mock_metrics: MagicMock) -> WALWriter:
    """Create a WALWriter pointed at tmp_path with mocked metrics."""
    return WALWriter(str(tmp_path))


# ---------- Test 1: valid JSONL round-trip ----------


@pytest.mark.asyncio
async def test_write_creates_valid_jsonl_file(
    tmp_path: Path, wal_writer: WALWriter
) -> None:
    """Write data via WALWriter, read back and verify each line is valid JSON."""
    data = [
        {"order_id": "O1", "price": 1001000, "qty": 10},
        {"order_id": "O2", "price": 2002000, "qty": 5},
    ]

    result = await wal_writer.write("orders", data)
    assert result is True

    jsonl_files = list(tmp_path.glob("orders_*.jsonl"))
    assert len(jsonl_files) == 1, "Expected exactly one WAL file"

    lines = jsonl_files[0].read_text().strip().splitlines()
    assert len(lines) == len(data)

    for i, line in enumerate(lines):
        parsed = _loads(line)
        assert parsed["order_id"] == data[i]["order_id"]
        assert parsed["price"] == data[i]["price"]
        assert parsed["qty"] == data[i]["qty"]


# ---------- Test 2: disk pressure skips write ----------


@pytest.mark.asyncio
async def test_disk_pressure_skips_write(
    tmp_path: Path, wal_writer: WALWriter
) -> None:
    """When disk is full and check interval hasn't elapsed, write returns False."""
    wal_writer._disk_full = True
    wal_writer._last_disk_check_ts = time.monotonic()  # recent check, won't re-check

    result = await wal_writer.write("orders", [{"order_id": "O1"}])
    assert result is False

    jsonl_files = list(tmp_path.glob("*.jsonl"))
    assert len(jsonl_files) == 0, "No file should be written when disk is full"
    assert wal_writer._disk_full_count == 1


# ---------- Test 3: disk pressure recovery ----------


def test_disk_pressure_recovery(
    tmp_path: Path, wal_writer: WALWriter
) -> None:
    """After disk was full, if statvfs shows enough space, _disk_full resets."""
    wal_writer._disk_full = True
    wal_writer._last_disk_check_ts = 0.0  # force re-check

    # Mock statvfs to report plenty of space
    mock_stat = MagicMock()
    mock_stat.f_bavail = 1_000_000  # blocks available
    mock_stat.f_frsize = 4096  # block size => ~3.8 GB available

    with patch("os.statvfs", return_value=mock_stat):
        ok = wal_writer._check_disk_space()

    assert ok is True
    assert wal_writer._disk_full is False


# ---------- Test 4: atomic write cleans up tmp on failure ----------


def test_atomic_write_cleans_up_on_fsync_failure(
    tmp_path: Path, _mock_metrics: MagicMock
) -> None:
    """If an IOError occurs during write, no .tmp file should remain."""
    # Re-enable fsync so _maybe_fsync_file actually calls os.fsync
    with patch.dict(os.environ, {"HFT_WAL_FILE_FSYNC": "1"}):
        writer = WALWriter(str(tmp_path))

    data = [{"order_id": "O1"}]
    filename = str(tmp_path / "test_table_123.jsonl")

    with patch("os.fsync", side_effect=IOError("disk I/O error")):
        with pytest.raises(IOError, match="disk I/O error"):
            writer._write_sync_atomic(filename, data)

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0, "Temp file must be cleaned up after failure"

    # The final .jsonl should also not exist (rename never happened)
    assert not (tmp_path / "test_table_123.jsonl").exists()


# ---------- Test 5: async write success ----------


@pytest.mark.asyncio
async def test_write_async_success(
    tmp_path: Path, wal_writer: WALWriter
) -> None:
    """Async write() with valid data returns True and creates a file."""
    data = [{"symbol": "2330", "price": 5500000}]

    result = await wal_writer.write("market_data", data)
    assert result is True

    jsonl_files = list(tmp_path.glob("market_data_*.jsonl"))
    assert len(jsonl_files) == 1

    content = _loads(jsonl_files[0].read_text().strip())
    assert content["symbol"] == "2330"
    assert content["price"] == 5500000


# ---------- Test 6: disk check interval caching ----------


def test_disk_check_interval_caching(
    tmp_path: Path, wal_writer: WALWriter
) -> None:
    """After first check, second check within interval returns cached result
    without calling statvfs again."""
    mock_stat = MagicMock()
    mock_stat.f_bavail = 1_000_000
    mock_stat.f_frsize = 4096

    with patch("os.statvfs", return_value=mock_stat) as patched_statvfs:
        # First call — should invoke statvfs (last_disk_check_ts == 0)
        result1 = wal_writer._check_disk_space()
        assert result1 is True
        assert patched_statvfs.call_count == 1

        # Second call within interval — should use cached result
        result2 = wal_writer._check_disk_space()
        assert result2 is True
        assert patched_statvfs.call_count == 1, (
            "statvfs should not be called again within the check interval"
        )
