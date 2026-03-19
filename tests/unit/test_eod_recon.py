import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.eod_recon import EODReconciliationRunner


class TestEOD:
    def _make(self, h=5):
        with patch.dict(os.environ, {"HFT_EOD_CLOSE_HOUR_UTC": str(h)}):
            oa = MagicMock()
            oa.drain_and_cancel = AsyncMock(return_value=3)
            return EODReconciliationRunner(oa, MagicMock(total_pnl=0, positions={}))

    @pytest.mark.asyncio
    async def test_triggers(self):
        r = self._make(5)
        now = datetime.datetime(2026, 3, 19, 5, 0, 0, tzinfo=datetime.timezone.utc)
        with patch("hft_platform.execution.eod_recon.datetime") as d:
            d.datetime.now.return_value = now
            d.timezone = datetime.timezone
            await r._check_trigger()
        assert r._last_triggered_day == now.toordinal()

    @pytest.mark.asyncio
    async def test_double_trigger(self):
        r = self._make(5)
        now = datetime.datetime(2026, 3, 19, 5, 0, 0, tzinfo=datetime.timezone.utc)
        with patch("hft_platform.execution.eod_recon.datetime") as d:
            d.datetime.now.return_value = now
            d.timezone = datetime.timezone
            await r._check_trigger()
            await r._check_trigger()
        assert r._order_adapter.drain_and_cancel.call_count == 1

    def test_stop(self):
        r = self._make()
        r._running = True
        r.stop()
        assert r._running is False
