"""Tests for EOD reconciliation runner (WU-04)."""

import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.eod_recon import EODReconciliationRunner


class TestEODReconciliationRunner:
    def _make_runner(self, eod_hour=5):
        with patch.dict(os.environ, {"HFT_EOD_CLOSE_HOUR_UTC": str(eod_hour)}):
            oa = MagicMock()
            oa.drain_and_cancel = AsyncMock(return_value=3)
            ps = MagicMock(total_pnl=50000, positions={"k": MagicMock()})
            return EODReconciliationRunner(oa, ps)

    @pytest.mark.asyncio
    async def test_triggers_at_correct_hour(self):
        r = self._make_runner(5)
        now = datetime.datetime(2026, 3, 19, 5, 0, 0, tzinfo=datetime.timezone.utc)
        with patch("hft_platform.execution.eod_recon.datetime") as dt:
            dt.datetime.now.return_value = now
            dt.timezone = datetime.timezone
            await r._check_trigger()
        assert r._last_triggered_day == now.toordinal()

    @pytest.mark.asyncio
    async def test_double_trigger_prevention(self):
        r = self._make_runner(5)
        now = datetime.datetime(2026, 3, 19, 5, 0, 0, tzinfo=datetime.timezone.utc)
        with patch("hft_platform.execution.eod_recon.datetime") as dt:
            dt.datetime.now.return_value = now
            dt.timezone = datetime.timezone
            await r._check_trigger()
            await r._check_trigger()
        assert r._order_adapter.drain_and_cancel.call_count == 1

    def test_stop(self):
        r = self._make_runner()
        r._running = True
        r.stop()
        assert r._running is False
