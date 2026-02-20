import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hft_platform.recorder.worker import RecorderService


class TestRecorderService(unittest.IsolatedAsyncioTestCase):
    async def test_worker_loop(self):
        queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            # Setup mock writer
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True
            mock_writer_inst.connect_async = AsyncMock()
            mock_writer_inst.write = AsyncMock()
            mock_writer_inst.write_columnar = AsyncMock()
            mock_writer_inst.shutdown = AsyncMock()
            mock_writer_inst.set_health_tracker = MagicMock()

            worker = RecorderService(queue)

            # Add items with correct schema
            await queue.put({"topic": "market_data", "data": {"price": 100}})
            await queue.put({"topic": "market_data", "data": {"price": 101}})

            task = asyncio.create_task(worker.run())

            # Allow loop to process items
            await asyncio.sleep(0.1)

            # Force flush manually since timing is flaky
            for b_name, b in worker.batchers.items():
                await b.force_flush()

            await asyncio.sleep(0.1)

            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # The worker calls batcher, batcher calls writer.write_columnar (or write)
            self.assertTrue(
                mock_writer_inst.write_columnar.called or mock_writer_inst.write.called
            )

    async def test_recover_wal_skips_when_disabled(self):
        queue = asyncio.Queue()

        with patch.dict(os.environ, {"HFT_DISABLE_CLICKHOUSE": "1"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter"):
                worker = RecorderService(queue)
                with patch("hft_platform.recorder.worker.logger.info") as log_info:
                    await worker.recover_wal()
                    # CE3-01: message now includes mode kwarg
                    calls = [str(c) for c in log_info.call_args_list]
                    assert any("Skipping WAL Recovery" in c for c in calls), (
                        f"Expected 'Skipping WAL Recovery' log, got: {calls}"
                    )

    async def test_recover_wal_warns_without_connection(self):
        queue = asyncio.Queue()

        class DummyLoader:
            def __init__(self):
                self.ch_client = None

            def connect(self):
                self.ch_client = None

            def process_files(self):
                raise AssertionError("process_files should not be called without ch_client")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "1"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter"):
                worker = RecorderService(queue)
                with patch("hft_platform.recorder.worker.asyncio.to_thread", new=fake_to_thread):
                    with patch(
                        "hft_platform.recorder.loader.WALLoaderService", new=DummyLoader
                    ):
                        with patch("hft_platform.recorder.worker.logger.warning") as log_warn:
                            await worker.recover_wal()
                            log_warn.assert_any_call(
                                "Skipping WAL Recovery (No ClickHouse Connection)"
                            )

    async def test_recover_wal_runs_when_connected(self):
        queue = asyncio.Queue()
        calls = SimpleNamespace(connect=0, process=0)

        class DummyLoader:
            def __init__(self):
                self.ch_client = object()

            def connect(self):
                calls.connect += 1

            def process_files(self):
                calls.process += 1

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "1"}, clear=False):
            with patch("hft_platform.recorder.worker.DataWriter"):
                worker = RecorderService(queue)
                with patch("hft_platform.recorder.worker.asyncio.to_thread", new=fake_to_thread):
                    with patch(
                        "hft_platform.recorder.loader.WALLoaderService", new=DummyLoader
                    ):
                        with patch("hft_platform.recorder.worker.logger.info") as log_info:
                            await worker.recover_wal()
                            self.assertGreaterEqual(calls.connect, 1)
                            self.assertGreaterEqual(calls.process, 1)
                            log_info.assert_any_call("Starting WAL Recovery...")
                            log_info.assert_any_call("WAL Recovery Complete")
