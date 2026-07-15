import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import TickEvent
from hft_platform.services.market_data import FeedState, MarketDataService


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
        self.client.login.return_value = True
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

    async def test_trigger_reconnect_exception_sets_disconnected(self):
        """B6: _trigger_reconnect() must catch exceptions from asyncio.to_thread and set DISCONNECTED."""
        self.service._last_reconnect_ts = 0.0
        self.service.reconnect_cooldown_s = 0.0
        # Force reconnect window open (empty strings → no window restriction)
        self.service.reconnect_days = set()
        self.service.reconnect_hours = ""
        self.service.reconnect_hours_2 = ""

        with patch(
            "hft_platform.services.market_data.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=Exception("sim API token_login failed"),
        ):
            result = await self.service._trigger_reconnect(gap=60.0, reason="heartbeat_gap")

        self.assertFalse(result)
        self.assertEqual(self.service.state, FeedState.DISCONNECTED)

    async def test_raw_queue_drop_escalates_to_stormguard(self):
        """Consecutive raw_queue drops must escalate to StormGuard DEGRADE then HALT."""
        storm_guard = MagicMock()
        storm_guard.trigger_storm = MagicMock()
        storm_guard.trigger_halt = MagicMock()
        self.service._storm_guard = storm_guard

        # Use a full queue (maxsize=1, pre-fill it)
        small_queue = asyncio.Queue(maxsize=1)
        small_queue.put_nowait(("dummy", "fill"))
        self.service.raw_queue = small_queue

        # Drop below degrade threshold — no escalation
        for _ in range(49):
            self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_storm.assert_not_called()
        storm_guard.trigger_halt.assert_not_called()

        # 50th drop crosses degrade threshold → trigger_storm called
        self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_storm.assert_called_once()
        storm_guard.trigger_halt.assert_not_called()

        # Continue dropping to halt threshold (200 total)
        for _ in range(150):
            self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_halt.assert_called_once()

        # Verify consecutive counter resets on successful enqueue
        # Drain queue and enqueue successfully
        small_queue.get_nowait()
        self.service._enqueue_raw("TSE", {"code": "2330"})
        self.assertEqual(self.service._raw_consecutive_drops, 0)

    async def test_raw_queue_drop_sliding_window_catches_intermittent_bursts(self):
        """Intermittent bursts below consecutive threshold must still escalate via sliding window.

        Scenario: 40 drops, 1 success, 40 drops — total 80 drops in ~0s.
        Consecutive counter resets to 0 on the success, so it never reaches 50.
        But the sliding window sees 80 drops in a short window and escalates.
        """
        storm_guard = MagicMock()
        storm_guard.trigger_storm = MagicMock()
        self.service._storm_guard = storm_guard

        # Configure sliding window: 60 drops in 5s window triggers STORM
        self.service._raw_drop_window_threshold = 60
        self.service._raw_drop_window_s = 5.0

        small_queue = asyncio.Queue(maxsize=1)
        small_queue.put_nowait(("dummy", "fill"))
        self.service.raw_queue = small_queue

        # Burst 1: 40 drops (below consecutive threshold of 50)
        for _ in range(40):
            self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_storm.assert_not_called()

        # 1 success — consecutive counter resets, but window counter should NOT
        small_queue.get_nowait()
        self.service._enqueue_raw("TSE", {"code": "2330"})
        self.assertEqual(self.service._raw_consecutive_drops, 0)
        # Queue is full again after successful put (maxsize=1), ready for burst 2

        # Burst 2: 40 more drops — total ~80 in window, exceeds threshold of 60
        for _ in range(40):
            self.service._enqueue_raw("TSE", {"code": "2330"})

        # Sliding window should have caught this
        storm_guard.trigger_storm.assert_called()
        # Verify the reason mentions "window" (not "consecutive")
        call_args = storm_guard.trigger_storm.call_args
        self.assertIn("window", str(call_args).lower())
