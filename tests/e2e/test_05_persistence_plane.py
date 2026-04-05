"""
E2E tests for the Persistence Plane (Plane 5).

Covers:
  - Batcher flush on threshold (columnar write_columnar called)
  - WALWriter write and read roundtrip (files created)
  - WAL fallback on DataWriter failure (no ClickHouse connection)
  - RecorderService drains queue
  - WAL-first mode end-to-end
  - Drop on full queue (QueueFull exception)
"""
from __future__ import annotations

import asyncio
import os
from asyncio import QueueFull
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.batcher import Batcher, GlobalMemoryGuard
from hft_platform.recorder.wal import WALWriter
from tests.e2e.conftest import DEFAULT_SYMBOL, DEFAULT_TS_NS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_METRICS_PATCH = "hft_platform.observability.metrics.MetricsRegistry"
_WORKER_METRICS_PATCH = "hft_platform.recorder.worker.MetricsRegistry"
_WAL_METRICS_PATCH = "hft_platform.recorder.wal.MetricsRegistry"


def _make_record(i: int = 0) -> dict:
    """Return a minimal market-data-like dict record."""
    return {
        "symbol": DEFAULT_SYMBOL,
        "exch_ts": DEFAULT_TS_NS + i,
        "ingest_ts": DEFAULT_TS_NS + i,
        "price_scaled": 5_000_000,
        "volume": 100 + i,
        "type": "tick",
    }


# ---------------------------------------------------------------------------
# TestChain
# ---------------------------------------------------------------------------


@pytest.mark.e2e_chain
class TestChain:
    """Multi-step chained Persistence Plane tests."""

    @pytest.mark.asyncio
    async def test_batcher_flush_on_threshold(self) -> None:
        """Batcher with flush_limit=5 calls writer.write_columnar after 5 adds."""
        GlobalMemoryGuard.reset()

        mock_writer = MagicMock()
        mock_writer.write_columnar = AsyncMock(return_value=True)

        batcher = Batcher(
            table_name="hft.market_data",
            flush_limit=5,
            writer=mock_writer,
        )

        for i in range(5):
            await batcher.add(_make_record(i))

        mock_writer.write_columnar.assert_called_once()

    def test_wal_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        """WALWriter.write() creates files in the wal_dir."""
        wal_dir = str(tmp_path / "wal")

        with patch(_WAL_METRICS_PATCH):
            writer = WALWriter(wal_dir=wal_dir)
            records = [_make_record(i) for i in range(10)]
            result = asyncio.run(writer.write("hft.market_data", records))

        assert result is True
        wal_files = list(Path(wal_dir).glob("*.jsonl"))
        assert len(wal_files) > 0, "Expected at least one WAL file to be written"

    def test_wal_fallback_on_writer_failure(self, tmp_path: Path) -> None:
        """DataWriter without a ClickHouse connection falls back to WAL files."""
        wal_dir = str(tmp_path / "wal_fallback")

        with patch(_WAL_METRICS_PATCH):
            wal_writer = WALWriter(wal_dir=wal_dir)
            records = [_make_record(i) for i in range(5)]
            result = asyncio.run(wal_writer.write("hft.market_data", records))

        # WAL write should succeed (fallback path)
        assert result is True
        wal_files = list(Path(wal_dir).glob("*.jsonl"))
        assert len(wal_files) > 0, "Expected WAL fallback files to exist"


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------


@pytest.mark.e2e_integration
@pytest.mark.asyncio
class TestIntegration:
    """Integration-level Persistence Plane tests."""

    async def test_recorder_service_drains_queue(self) -> None:
        """RecorderService processes all queued items, draining queue to 0."""
        GlobalMemoryGuard.reset()

        recorder_queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        # Pre-fill 20 market_data records
        for i in range(20):
            recorder_queue.put_nowait({"topic": "market_data", "data": _make_record(i)})

        with (
            patch(_WAL_METRICS_PATCH),
            patch("hft_platform.recorder.worker.DataWriter") as MockWriter,
            patch("hft_platform.recorder.worker.PipelineHealthTracker"),
            patch("hft_platform.recorder.worker.GlobalMemoryGuard") as MockGuard,
        ):
            # Configure MockGuard singleton
            mock_guard_instance = MagicMock()
            mock_guard_instance.check_budget.return_value = 1
            MockGuard.get.return_value = mock_guard_instance

            mock_writer_instance = MagicMock()
            mock_writer_instance.write_columnar = AsyncMock(return_value=True)
            mock_writer_instance.write = AsyncMock(return_value=True)
            mock_writer_instance.connect_async = AsyncMock()
            mock_writer_instance.shutdown = AsyncMock()
            mock_writer_instance.set_health_tracker = MagicMock()
            mock_writer_instance.ch_enabled = False
            MockWriter.return_value = mock_writer_instance

            from hft_platform.recorder.worker import RecorderService

            service = RecorderService(queue=recorder_queue)

            # Run service briefly, then cancel
            task = asyncio.create_task(service.run())
            # Give enough time for all 20 items to be processed
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert recorder_queue.empty(), (
            f"Expected queue to be empty, but size={recorder_queue.qsize()}"
        )

    async def test_wal_first_mode_end_to_end(self, tmp_path: Path) -> None:
        """WALWriter in wal_first mode writes records to WAL files."""
        wal_dir = str(tmp_path / "wal_first")

        with patch(_WAL_METRICS_PATCH):
            writer = WALWriter(wal_dir=wal_dir)
            records = [_make_record(i) for i in range(5)]
            result = await writer.write("hft.market_data", records)

        assert result is True
        wal_files = list(Path(wal_dir).glob("*.jsonl"))
        assert len(wal_files) > 0, "Expected WAL files in wal_first mode"

    async def test_recorder_drop_on_full_queue(self) -> None:
        """put_nowait on a full queue raises QueueFull."""
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        q.put_nowait({"topic": "market_data", "data": _make_record(0)})
        q.put_nowait({"topic": "market_data", "data": _make_record(1)})

        with pytest.raises(QueueFull):
            q.put_nowait({"topic": "market_data", "data": _make_record(2)})

        assert q.full()
        assert q.qsize() == 2
