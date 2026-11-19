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
        task = asyncio.create_task(self.service.run())
        await asyncio.sleep(0.01)

        self.assertTrue(self.service.running)
        self.assertTrue(self.client.subscribe_basket.called)

        self.service.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_on_tick_processing(self):
        """Verify tick normalization and bus publication."""
        mock_tick = {"code": "2330", "close": 100.0, "volume": 1}

        await self.raw_queue.put(mock_tick)

        task = asyncio.create_task(self.service.run())
        await asyncio.sleep(0.01)
        self.service.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        lob = self.service.lob.get_book("2330")
        self.assertIsNotNone(lob)

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

        small_queue = asyncio.Queue(maxsize=1)
        small_queue.put_nowait(("dummy", "fill"))
        self.service.raw_queue = small_queue

        for _ in range(49):
            self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_storm.assert_not_called()
        storm_guard.trigger_halt.assert_not_called()

        self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_storm.assert_called_once()
        storm_guard.trigger_halt.assert_not_called()

        for _ in range(150):
            self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_halt.assert_called_once()

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

        self.service._raw_drop_window_threshold = 60
        self.service._raw_drop_window_s = 5.0

        small_queue = asyncio.Queue(maxsize=1)
        small_queue.put_nowait(("dummy", "fill"))
        self.service.raw_queue = small_queue

        for _ in range(40):
            self.service._enqueue_raw("TSE", {"code": "2330"})
        storm_guard.trigger_storm.assert_not_called()

        small_queue.get_nowait()
        self.service._enqueue_raw("TSE", {"code": "2330"})
        self.assertEqual(self.service._raw_consecutive_drops, 0)

        for _ in range(40):
            self.service._enqueue_raw("TSE", {"code": "2330"})

        storm_guard.trigger_storm.assert_called()
        call_args = storm_guard.trigger_storm.call_args
        self.assertIn("window", str(call_args).lower())


class TestMarketDataConsumerYields(unittest.IsolatedAsyncioTestCase):
    """A queued burst must not hold the event loop for its whole length.

    ``asyncio.Queue.get()`` returns without suspending while the queue is
    non-empty, so a consumer that only awaits ``get()`` drains a burst in one
    loop step. On THESHOW that blocked execution reports for 25-50 ms during the
    day session (ExecutionLagHigh, 2026-10-21).
    """

    async def asyncSetUp(self):
        self._old_symbols_config = os.environ.get("SYMBOLS_CONFIG")
        self._tmp_dir = tempfile.TemporaryDirectory()
        cfg_path = Path(self._tmp_dir.name) / "symbols.yaml"
        cfg_path.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
        os.environ["SYMBOLS_CONFIG"] = str(cfg_path)
        self.raw_queue = asyncio.Queue()
        client = MagicMock()
        client.login.return_value = True
        self.service = MarketDataService(MagicMock(spec=RingBufferBus), self.raw_queue, client)

    async def asyncTearDown(self):
        if self._old_symbols_config is None:
            os.environ.pop("SYMBOLS_CONFIG", None)
        else:
            os.environ["SYMBOLS_CONFIG"] = self._old_symbols_config
        self._tmp_dir.cleanup()

    async def _drain_burst(self, n: int) -> list[int]:
        """Process ``n`` queued messages; return the other task's step count seen by each."""
        other_steps = 0
        seen: list[int] = []
        drained = asyncio.Event()

        def _process(_raw):
            seen.append(other_steps)
            if len(seen) == n:
                drained.set()

        async def _other_task():
            nonlocal other_steps
            while not drained.is_set():
                other_steps += 1
                await asyncio.sleep(0)

        for i in range(n):
            self.raw_queue.put_nowait({"code": "2330", "close": 100.0 + i, "volume": 1})
        with (
            patch.object(self.service, "_process_raw", side_effect=_process),
            patch.object(self.service, "_connect_sequence", AsyncMock()),
        ):
            other = asyncio.create_task(_other_task())
            task = asyncio.create_task(self.service.run())
            await asyncio.wait_for(drained.wait(), timeout=5)
            self.service.running = False
            task.cancel()
            other.cancel()
            await asyncio.gather(task, other, return_exceptions=True)
        return seen

    async def test_consumer_yields_within_a_queued_burst(self):
        seen = await self._drain_burst(64)
        longest_run = max(seen.count(v) for v in set(seen))
        self.assertEqual(len(seen), 64)
        self.assertEqual(longest_run, 1)

    async def test_consumer_yield_every_env_still_batches_a_burst(self):
        with patch.dict(os.environ, {"HFT_MD_YIELD_EVERY": "8"}):
            client = MagicMock()
            client.login.return_value = True
            self.service = MarketDataService(MagicMock(spec=RingBufferBus), self.raw_queue, client)
        seen = await self._drain_burst(64)
        longest_run = max(seen.count(v) for v in set(seen))
        self.assertEqual(len(seen), 64)
        self.assertGreater(longest_run, 1)
        self.assertLessEqual(longest_run, 8)

    async def test_consumer_yield_every_is_bounded_below_by_one(self):
        with patch.dict(os.environ, {"HFT_MD_YIELD_EVERY": "0"}):
            client = MagicMock()
            client.login.return_value = True
            svc = MarketDataService(MagicMock(spec=RingBufferBus), asyncio.Queue(), client)
        self.assertEqual(svc._md_yield_every, 1)
