"""Tests for CE3-02: WALFirstWriter."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.disk_monitor import DiskPressureLevel, DiskPressureMonitor
from hft_platform.recorder.wal_first import WALFirstWriter


def _make_writer(level: DiskPressureLevel = DiskPressureLevel.OK, policy: str = "write"):
    wal_batch = AsyncMock()
    wal_batch.add = AsyncMock(return_value=True)

    disk_monitor = MagicMock(spec=DiskPressureMonitor)
    disk_monitor.get_level.return_value = level
    disk_monitor.get_topic_policy.return_value = policy

    writer = WALFirstWriter(wal_batch, disk_monitor)
    return writer, wal_batch


@pytest.mark.asyncio
async def test_wal_first_writes_ok_level():
    writer, wal_batch = _make_writer(DiskPressureLevel.OK)
    result = await writer.write("market_data", [{"x": 1}])
    assert result is True
    wal_batch.add.assert_called_once()


@pytest.mark.asyncio
async def test_wal_first_writes_warn_level():
    writer, wal_batch = _make_writer(DiskPressureLevel.WARN)
    result = await writer.write("market_data", [{"x": 1}])
    assert result is True
    wal_batch.add.assert_called_once()


@pytest.mark.asyncio
async def test_wal_first_halt_level_returns_false():
    writer, wal_batch = _make_writer(DiskPressureLevel.HALT)
    result = await writer.write("market_data", [{"x": 1}])
    assert result is False
    wal_batch.add.assert_not_called()


@pytest.mark.asyncio
async def test_wal_first_critical_drop_policy():
    writer, wal_batch = _make_writer(DiskPressureLevel.CRITICAL, policy="drop")
    result = await writer.write("latency_spans", [{"x": 1}])
    assert result is False
    wal_batch.add.assert_not_called()


@pytest.mark.asyncio
async def test_wal_first_critical_halt_policy():
    writer, wal_batch = _make_writer(DiskPressureLevel.CRITICAL, policy="halt")
    result = await writer.write("latency_spans", [{"x": 1}])
    assert result is False
    wal_batch.add.assert_not_called()


@pytest.mark.asyncio
async def test_wal_first_critical_write_policy_still_writes():
    writer, wal_batch = _make_writer(DiskPressureLevel.CRITICAL, policy="write")
    result = await writer.write("market_data", [{"x": 1}])
    assert result is True
    wal_batch.add.assert_called_once()


@pytest.mark.asyncio
async def test_wal_first_no_clickhouse_calls():
    """Verify WALFirstWriter never tries to call ClickHouse."""
    writer, wal_batch = _make_writer()
    # Monkey-patch: if ClickHouse were ever imported, fail
    with patch("clickhouse_connect.get_client", side_effect=AssertionError("CH should not be called")):
        result = await writer.write("orders", [{"order_id": "X"}])
    assert result is True


@pytest.mark.asyncio
async def test_wal_first_flush():
    writer, wal_batch = _make_writer()
    await writer.flush()
    wal_batch.flush.assert_called_once()
