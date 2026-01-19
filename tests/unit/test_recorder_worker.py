import asyncio
import unittest
from unittest.mock import patch

from hft_platform.recorder.worker import RecorderService


class TestRecorderService(unittest.IsolatedAsyncioTestCase):
    async def test_worker_loop(self):
        queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            # Setup mock writer
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True

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

            # We need to verify calls on the MOCK instance
            # The worker calls batcher, batcher calls writer.write
            # The MockWriter class mock returns mock_writer_inst when instantiated.
            self.assertTrue(mock_writer_inst.write.called)
