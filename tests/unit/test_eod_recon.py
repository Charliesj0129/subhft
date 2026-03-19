"""Tests for EOD reconciliation runner (WU-01)."""

from unittest.mock import AsyncMock, patch

import pytest

from hft_platform.execution.eod_recon import EODReconciliationRunner


class TestEODReconciliationRunner:
    def _make_runner(self, hour: int = 5) -> EODReconciliationRunner:
        recon = AsyncMock()
        recon.sync_portfolio = AsyncMock()
        return EODReconciliationRunner(recon, close_hour_utc=hour)

    @pytest.mark.asyncio
    async def test_triggers_at_correct_hour(self):
        r = self._make_runner(5)
        with patch(
            "hft_platform.execution.eod_recon._current_utc_hour_and_date",
            return_value=(5, "2026-03-20"),
        ):
            await r._check_and_trigger()
        r._recon_service.sync_portfolio.assert_awaited_once()
        assert r._last_trigger_date == "2026-03-20"

    @pytest.mark.asyncio
    async def test_skips_wrong_hour(self):
        r = self._make_runner(5)
        with patch(
            "hft_platform.execution.eod_recon._current_utc_hour_and_date",
            return_value=(10, "2026-03-20"),
        ):
            await r._check_and_trigger()
        r._recon_service.sync_portfolio.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_once_per_day_guard(self):
        r = self._make_runner(5)
        with patch(
            "hft_platform.execution.eod_recon._current_utc_hour_and_date",
            return_value=(5, "2026-03-20"),
        ):
            await r._check_and_trigger()
            await r._check_and_trigger()
        assert r._recon_service.sync_portfolio.await_count == 1

    @pytest.mark.asyncio
    async def test_triggers_again_on_new_day(self):
        r = self._make_runner(5)
        with patch(
            "hft_platform.execution.eod_recon._current_utc_hour_and_date",
            return_value=(5, "2026-03-20"),
        ):
            await r._check_and_trigger()
        with patch(
            "hft_platform.execution.eod_recon._current_utc_hour_and_date",
            return_value=(5, "2026-03-21"),
        ):
            await r._check_and_trigger()
        assert r._recon_service.sync_portfolio.await_count == 2

    @pytest.mark.asyncio
    async def test_metrics_on_success(self):
        r = self._make_runner(5)
        with patch(
            "hft_platform.execution.eod_recon._current_utc_hour_and_date",
            return_value=(5, "2026-03-20"),
        ):
            await r._check_and_trigger()
        assert r._eod_recon_status._value.get() == 1  # SUCCESS

    @pytest.mark.asyncio
    async def test_metrics_on_failure(self):
        r = self._make_runner(5)
        r._recon_service.sync_portfolio = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(
            "hft_platform.execution.eod_recon._current_utc_hour_and_date",
            return_value=(5, "2026-03-20"),
        ):
            await r._check_and_trigger()
        assert r._eod_recon_status._value.get() == 2  # FAILURE

    def test_stop(self):
        r = self._make_runner()
        r.running = True
        r.stop()
        assert r.running is False

    def test_configurable_hour(self):
        r = self._make_runner(13)
        assert r.close_hour_utc == 13
