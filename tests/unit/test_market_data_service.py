import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import TickEvent
from hft_platform.services.market_data import MarketDataService


class TestMarketDataService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_symbols_config = os.environ.get("SYMBOLS_CONFIG")
        self._tmp_dir = tempfile.TemporaryDirectory()
        cfg_path = Path(self._tmp_dir.name) / "symbols.yaml"
        cfg_path.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
        os.environ["SYMBOLS_CONFIG"] = str(cfg_path)

        self.bus = MagicMock(spec=RingBufferBus)
        self.raw_queue = asyncio.Queue()
        self.client = MagicMock()
        self.service = MarketDataService(self.bus, self.raw_queue, self.client)

    async def asyncTearDown(self):
        if self._old_symbols_config is None:
            os.environ.pop("SYMBOLS_CONFIG", None)
        else:
            os.environ["SYMBOLS_CONFIG"] = self._old_symbols_config
        self._tmp_dir.cleanup()

    async def test_start_stop(self):
        """Verify startup and shutdown lifecycle."""
        # Start in background
        task = asyncio.create_task(self.service.run())
        await asyncio.sleep(0.01)

        self.assertTrue(self.service.running)
        self.assertTrue(self.client.subscribe_basket.called)

        # Stop
        self.service.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_on_tick_processing(self):
        """Verify tick normalization and bus publication."""
        # Create a mock tick
        mock_tick = {"code": "2330", "close": 100.0, "volume": 1}

        # Inject into queue
        await self.raw_queue.put(mock_tick)

        # Run loop briefly
        task = asyncio.create_task(self.service.run())
        await asyncio.sleep(0.01)
        self.service.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify LOB update
        lob = self.service.lob.get_book("2330")
        self.assertIsNotNone(lob)

        # Verify Bus publish
        # normalizer publishes NormalizedTickEvent
        # Check call args of bus.publish
        # self.bus.publish.assert_called()
        # Actually normalizer calls bus.publish directly?
        # No, normalizer returns event, MarketDataService loop publishes it?
        # Let's check service logic...
        # Service: while running: msg = await queue.get(); events = normalizer.normalize(msg); for e in events: bus.publish(e)

        self.bus.publish_nowait.assert_called()
        call_args = self.bus.publish_nowait.call_args[0][0]
        self.assertIsInstance(call_args, TickEvent)
        self.assertEqual(call_args.symbol, "2330")

    async def test_snapshot_bootstrap(self):
        self.client.fetch_snapshots.return_value = [
            {
                "code": "2330",
                "buy_price": 100.0,
                "buy_volume": 2,
                "sell_price": 101.0,
                "sell_volume": 3,
            }
        ]

        await self.service._connect_sequence()

        book = self.service.lob.get_book("2330")
        self.assertEqual(book.bids[0][0], 1000000)
        self.assertEqual(book.asks[0][0], 1010000)
