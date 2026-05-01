"""Regression: in wal_first mode, engine must report CH health as healthy.

Root cause context: `recorder/worker.py:run()` gates `writer.connect_async()`
on `self._mode != RecorderMode.WAL_FIRST`. In wal_first mode the CH connect
path never runs, so `DataWriter` never calls `set(1)` or `set(0)` on the
`clickhouse_connection_health` gauge. The gauge is default-initialised to 0
and stays there forever, permanently triggering ClickHouseConnectionDown
even though the engine's recorder is healthy (it writes WAL, which the
WAL-loader container ingests into ClickHouse).

This test verifies that when the recorder starts in wal_first mode, the
engine-side gauge is set to 1 at run() startup as a "no-op healthy" signal.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from hft_platform.recorder.worker import RecorderService


class TestWalFirstCHHealthGauge(unittest.IsolatedAsyncioTestCase):
    async def test_run_sets_ch_health_to_1_in_wal_first_mode(self):
        queue: asyncio.Queue = asyncio.Queue()

        with patch.dict(os.environ, {"HFT_RECORDER_MODE": "wal_first"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
                mock_writer_inst = MockWriter.return_value
                mock_writer_inst.active = True
                mock_writer_inst.connect_async = AsyncMock()
                mock_writer_inst.write = AsyncMock()
                mock_writer_inst.write_columnar = AsyncMock()
                mock_writer_inst.shutdown = AsyncMock()
                mock_writer_inst.set_health_tracker = MagicMock()

                fake_registry = MagicMock()

                with patch(
                    "hft_platform.observability.metrics.MetricsRegistry.get",
                    return_value=fake_registry,
                ):
                    # DiskPressureMonitor/WALBatchWriter/WALFirstWriter are
                    # imported lazily inside run(); patch at source module.
                    with patch("hft_platform.recorder.disk_monitor.DiskPressureMonitor"):
                        with patch("hft_platform.recorder.wal.WALBatchWriter"):
                            with patch("hft_platform.recorder.wal_first.WALFirstWriter"):
                                worker = RecorderService(queue)
                                task = asyncio.create_task(worker.run())
                                # Give run() enough time to execute the startup block.
                                await asyncio.sleep(0.05)
                                worker.running = False
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass

        # In wal_first mode the writer never connect_async()es, so DataWriter
        # never fires the gauge setter. The fix must set it to 1 at run()
        # startup as a healthy no-op signal.
        fake_registry.clickhouse_connection_health.set.assert_called_with(1)
        # Sanity: this is the wal_first path.
        fake_registry.wal_mode.set.assert_called_with(1)
        # Sanity: writer.connect_async was NOT called in wal_first mode.
        mock_writer_inst.connect_async.assert_not_called()

    async def test_run_does_not_force_ch_health_in_direct_mode(self):
        """In non-wal_first mode, worker.run() must NOT pre-set CH gauge to 1.

        DataWriter.connect_async() owns the gauge in direct-CH mode — it sets
        it to 1 on successful connect and 0 on failure/heartbeat breakdown.
        Forcing the gauge to 1 at startup would mask connect failures.
        """
        queue: asyncio.Queue = asyncio.Queue()

        with patch.dict(os.environ, {"HFT_RECORDER_MODE": "direct"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
                mock_writer_inst = MockWriter.return_value
                mock_writer_inst.active = True
                mock_writer_inst.connect_async = AsyncMock()
                mock_writer_inst.write = AsyncMock()
                mock_writer_inst.write_columnar = AsyncMock()
                mock_writer_inst.shutdown = AsyncMock()
                mock_writer_inst.set_health_tracker = MagicMock()

                fake_registry = MagicMock()

                with patch(
                    "hft_platform.observability.metrics.MetricsRegistry.get",
                    return_value=fake_registry,
                ):
                    worker = RecorderService(queue)
                    task = asyncio.create_task(worker.run())
                    await asyncio.sleep(0.05)
                    worker.running = False
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        # In direct mode the fix must NOT touch the CH gauge at startup; that
        # is DataWriter.connect_async()'s responsibility.
        set_calls = [call.args for call in fake_registry.clickhouse_connection_health.set.call_args_list]
        assert set_calls == [], (
            f"worker.run() must not set clickhouse_connection_health in direct mode; observed calls: {set_calls}"
        )
        fake_registry.wal_mode.set.assert_called_with(0)
