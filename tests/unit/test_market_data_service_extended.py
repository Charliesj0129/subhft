import asyncio
import os
import tempfile
import unittest
import datetime as dt
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.services.market_data import FeedState, MarketDataService


class TestMarketDataServiceExtended(unittest.IsolatedAsyncioTestCase):
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

    async def test_publish_fallback_paths(self):
        bus = MagicMock(spec=RingBufferBus)
        bus.publish_nowait = None
        bus.publish = MagicMock(return_value=None)
        service = MarketDataService(bus, self.raw_queue, self.client)

        service._publish_nowait("evt")
        await asyncio.sleep(0)
        bus.publish.assert_called_once_with("evt")

        bus.publish.reset_mock()
        bus.publish_many_nowait = MagicMock()
        service._publish_many_nowait(["a", "b"])
        bus.publish_many_nowait.assert_called_once_with(["a", "b"])

    async def test_connect_sequence_success(self):
        self.client.fetch_snapshots.return_value = []
        await self.service._connect_sequence()
        self.client.login.assert_called_once()
        self.client.validate_symbols.assert_called_once()
        self.client.subscribe_basket.assert_called_once()
        self.assertEqual(self.service.state, FeedState.CONNECTED)

    async def test_connect_sequence_failure(self):
        self.client.login.side_effect = RuntimeError("boom")
        await self.service._connect_sequence()
        self.assertEqual(self.service.state, FeedState.DISCONNECTED)

    async def test_publish_no_publish_fn(self):
        service = MarketDataService(object(), self.raw_queue, self.client)
        await service._publish("evt")

    async def test_publish_awaits_coroutine(self):
        called = {"ok": False}

        async def _pub(_evt):
            called["ok"] = True

        bus = MagicMock(spec=RingBufferBus)
        bus.publish = _pub
        service = MarketDataService(bus, self.raw_queue, self.client)
        await service._publish("evt")
        self.assertTrue(called["ok"])

    async def test_publish_many_nowait_fallback(self):
        bus = MagicMock(spec=RingBufferBus)
        bus.publish_many_nowait = None
        bus.publish_nowait = MagicMock()
        service = MarketDataService(bus, self.raw_queue, self.client)
        service._publish_many_nowait(["a", "b", "c"])
        self.assertEqual(bus.publish_nowait.call_count, 3)

    async def test_attempt_resubscribe_resets_on_success(self):
        self.client.resubscribe.return_value = True
        self.service._resubscribe_attempts = 2
        with patch.object(self.service, "_within_reconnect_window", return_value=True):
            await self.service._attempt_resubscribe(10.0)
        self.assertEqual(self.service._resubscribe_attempts, 0)

    async def test_attempt_resubscribe_increments_on_fail(self):
        self.client.resubscribe.return_value = False
        self.service._resubscribe_attempts = 1
        with patch.object(self.service, "_within_reconnect_window", return_value=True):
            await self.service._attempt_resubscribe(10.0)
        self.assertEqual(self.service._resubscribe_attempts, 2)

    async def test_attempt_resubscribe_window_block(self):
        self.client.resubscribe.return_value = True
        self.service._resubscribe_attempts = 1
        with patch.object(self.service, "_within_reconnect_window", return_value=False):
            await self.service._attempt_resubscribe(10.0)
        self.client.resubscribe.assert_not_called()
        self.assertEqual(self.service._resubscribe_attempts, 1)

    async def test_trigger_reconnect_respects_cooldown(self):
        self.service._set_state(FeedState.CONNECTED)
        self.service._last_reconnect_ts = time.time()
        self.service.reconnect_cooldown_s = 3600
        with patch.object(self.service, "_within_reconnect_window", return_value=True):
            await self.service._trigger_reconnect(9.0)
        self.client.reconnect.assert_not_called()

    async def test_trigger_reconnect_success(self):
        self.service._set_state(FeedState.CONNECTED)
        self.service._last_reconnect_ts = 0.0
        self.service.reconnect_cooldown_s = 0.0
        self.client.reconnect.return_value = True
        with patch.object(self.service, "_within_reconnect_window", return_value=True):
            await self.service._trigger_reconnect(9.0)
        self.assertEqual(self.service.state, FeedState.CONNECTED)

    async def test_trigger_reconnect_failure(self):
        self.service._set_state(FeedState.CONNECTED)
        self.service._last_reconnect_ts = 0.0
        self.service.reconnect_cooldown_s = 0.0
        self.client.reconnect.return_value = False
        with patch.object(self.service, "_within_reconnect_window", return_value=True):
            await self.service._trigger_reconnect(9.0)
        self.assertEqual(self.service.state, FeedState.DISCONNECTED)

    async def test_trigger_reconnect_window_block(self):
        self.service._set_state(FeedState.CONNECTED)
        self.service._last_reconnect_ts = 0.0
        self.service.reconnect_cooldown_s = 0.0
        with patch.object(self.service, "_within_reconnect_window", return_value=False):
            await self.service._trigger_reconnect(9.0)
        self.client.reconnect.assert_not_called()

    def test_within_reconnect_window_unrestricted(self):
        self.service.reconnect_days = set()
        self.service.reconnect_hours = ""
        self.service.reconnect_hours_2 = ""
        assert self.service._within_reconnect_window() is True

    def test_within_reconnect_window_day_mismatch(self):
        self.service.reconnect_days = {"mon"}
        self.service.reconnect_hours = ""
        self.service.reconnect_hours_2 = ""
        now = dt.datetime(2026, 2, 3, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with patch("hft_platform.services.market_data.dt.datetime") as mock_dt:
            mock_dt.now.return_value = now
            self.assertFalse(self.service._within_reconnect_window())

    def test_within_reconnect_window_hours(self):
        self.service.reconnect_days = set()
        self.service.reconnect_hours = "09:00-15:00"
        self.service.reconnect_hours_2 = ""
        now = dt.datetime(2026, 2, 3, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with patch("hft_platform.services.market_data.dt.datetime") as mock_dt:
            mock_dt.now.return_value = now
            self.assertTrue(self.service._within_reconnect_window())

    def test_within_reconnect_window_overnight(self):
        self.service.reconnect_days = set()
        self.service.reconnect_hours = "22:00-02:00"
        self.service.reconnect_hours_2 = ""
        late = dt.datetime(2026, 2, 3, 23, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        early = dt.datetime(2026, 2, 4, 1, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with patch("hft_platform.services.market_data.dt.datetime") as mock_dt:
            mock_dt.now.return_value = late
            self.assertTrue(self.service._within_reconnect_window())
        with patch("hft_platform.services.market_data.dt.datetime") as mock_dt:
            mock_dt.now.return_value = early
            self.assertTrue(self.service._within_reconnect_window())

    def test_within_reconnect_window_invalid_window(self):
        self.service.reconnect_days = set()
        self.service.reconnect_hours = "bad"
        self.service.reconnect_hours_2 = ""
        now = dt.datetime(2026, 2, 3, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with patch("hft_platform.services.market_data.dt.datetime") as mock_dt:
            mock_dt.now.return_value = now
            self.assertFalse(self.service._within_reconnect_window())
