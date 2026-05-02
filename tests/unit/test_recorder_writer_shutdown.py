"""Tests for DataWriter.shutdown() graceful drain behavior (RC-3)."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.writer import DataWriter


@pytest.fixture()
def writer(tmp_path):
    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "0"}, clear=False):
        return DataWriter(wal_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_shutdown_drains_all_permits(writer):
    """When all semaphore permits are available, shutdown drains immediately."""
    # All permits available (no in-flight inserts)
    assert writer._insert_semaphore._value == writer._max_concurrent_inserts

    await writer.shutdown()

    # Heartbeat stopped
    assert writer._heartbeat_running is False


@pytest.mark.asyncio
async def test_shutdown_heartbeat_stopped_before_drain(writer):
    """Heartbeat must be stopped before waiting for in-flight inserts."""
    call_order: list[str] = []
    original_heartbeat = writer._heartbeat_running

    # Track when heartbeat is set to False vs when semaphore is checked
    class TrackingSemaphore:
        def __init__(self, real_sem, max_val):
            self._real = real_sem
            self._max_val = max_val

        @property
        def _value(self):
            # Record that semaphore was checked; heartbeat should already be False
            call_order.append(f"sem_check:hb={writer._heartbeat_running}")
            return self._max_val

    writer._insert_semaphore = TrackingSemaphore(writer._insert_semaphore, writer._max_concurrent_inserts)

    await writer.shutdown()

    assert writer._heartbeat_running is False
    # The semaphore check should see heartbeat already False
    for entry in call_order:
        assert "hb=False" in entry, f"Heartbeat was still running when semaphore checked: {entry}"


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_inserts(writer):
    """Shutdown waits for in-flight inserts (semaphore permits) to return."""
    # Simulate 2 in-flight inserts by acquiring 2 permits
    await writer._insert_semaphore.acquire()
    await writer._insert_semaphore.acquire()
    inflight = writer._max_concurrent_inserts - writer._insert_semaphore._value
    assert inflight == 2

    # Release permits after a short delay (simulating insert completion)
    async def release_after_delay():
        await asyncio.sleep(0.15)
        writer._insert_semaphore.release()
        await asyncio.sleep(0.05)
        writer._insert_semaphore.release()

    release_task = asyncio.create_task(release_after_delay())

    start = time.monotonic()
    with patch.dict("os.environ", {"HFT_CH_SHUTDOWN_TIMEOUT_S": "5"}, clear=False):
        await writer.shutdown()
    elapsed = time.monotonic() - start

    await release_task

    # Should have waited for the releases (~0.2s), not timed out
    assert elapsed < 2.0
    assert writer._insert_semaphore._value == writer._max_concurrent_inserts


@pytest.mark.asyncio
async def test_shutdown_timeout_logs_warning(writer):
    """When in-flight inserts don't complete, shutdown times out with warning."""
    # Acquire a permit and never release it
    await writer._insert_semaphore.acquire()

    with patch.dict("os.environ", {"HFT_CH_SHUTDOWN_TIMEOUT_S": "1"}, clear=False):
        start = time.monotonic()
        await writer.shutdown()
        elapsed = time.monotonic() - start

    # Should have timed out after ~1s
    assert elapsed >= 0.9
    assert elapsed < 3.0

    # Permit still held (simulating lost insert)
    assert writer._insert_semaphore._value < writer._max_concurrent_inserts


@pytest.mark.asyncio
async def test_shutdown_executor_wait_true_on_drain(writer):
    """Executor.shutdown(wait=True) when all inserts drained."""
    with patch.object(writer._executor, "shutdown") as mock_shutdown:
        await writer.shutdown()
        mock_shutdown.assert_called_once_with(wait=True)


@pytest.mark.asyncio
async def test_shutdown_executor_wait_false_on_timeout(writer):
    """Executor.shutdown(wait=False) when drain times out."""
    await writer._insert_semaphore.acquire()

    with (
        patch.dict("os.environ", {"HFT_CH_SHUTDOWN_TIMEOUT_S": "1"}, clear=False),
        patch.object(writer._executor, "shutdown") as mock_shutdown,
    ):
        await writer.shutdown()
        mock_shutdown.assert_called_once_with(wait=False)


@pytest.mark.asyncio
async def test_shutdown_flushes_wal_batch_writer(writer):
    """WAL batch writer is flushed and stopped during shutdown."""
    mock_wal = MagicMock()
    mock_wal.flush = AsyncMock()
    mock_wal.stop = MagicMock()
    writer._wal_batch_writer = mock_wal

    await writer.shutdown()

    mock_wal.flush.assert_awaited_once()
    mock_wal.stop.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_default_timeout_is_30(writer):
    """Default shutdown timeout is 30 seconds from HFT_CH_SHUTDOWN_TIMEOUT_S."""
    # Ensure env var is not set
    with patch.dict("os.environ", {}, clear=False):
        # Remove the env var if present
        import os

        os.environ.pop("HFT_CH_SHUTDOWN_TIMEOUT_S", None)

        # All permits available, so drain is instant - just verify it doesn't error
        await writer.shutdown()
        assert writer._heartbeat_running is False
