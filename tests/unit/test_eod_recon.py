"""Tests for EODReconciliationRunner (WU-01)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.eod_recon import (
    _STATUS_FAILURE,
    _STATUS_PENDING,
    _STATUS_SUCCESS,
    EODReconciliationRunner,
)


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset MetricsRegistry singleton so each test gets fresh gauges."""
    from prometheus_client import REGISTRY

    from hft_platform.observability.metrics import MetricsRegistry

    if MetricsRegistry._instance is None:
        collectors = list(REGISTRY._names_to_collectors.values())
        for c in set(collectors):
            try:
                REGISTRY.unregister(c)
            except KeyError:
                pass
        MetricsRegistry._instance = MetricsRegistry()
    yield


def _make_runner(
    close_hour: int = 5,
    sync_side_effect: object | None = None,
) -> tuple[EODReconciliationRunner, AsyncMock]:
    recon = MagicMock()
    recon.sync_portfolio = AsyncMock(side_effect=sync_side_effect)
    runner = EODReconciliationRunner(recon_service=recon, close_hour_utc=close_hour)
    return runner, recon.sync_portfolio


@pytest.mark.asyncio
async def test_triggers_sync_at_correct_hour():
    runner, sync_mock = _make_runner(close_hour=5)
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(5, "2026-03-19"),
    ):
        await runner._check_and_trigger()
    sync_mock.assert_awaited_once()
    assert runner._eod_recon_status._value.get() == _STATUS_SUCCESS


@pytest.mark.asyncio
async def test_skips_wrong_hour():
    runner, sync_mock = _make_runner(close_hour=5)
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(10, "2026-03-19"),
    ):
        await runner._check_and_trigger()
    sync_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_once_per_day_guard():
    runner, sync_mock = _make_runner(close_hour=5)
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(5, "2026-03-19"),
    ):
        await runner._check_and_trigger()
        await runner._check_and_trigger()
    assert sync_mock.await_count == 1


@pytest.mark.asyncio
async def test_triggers_again_on_new_day():
    runner, sync_mock = _make_runner(close_hour=5)
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(5, "2026-03-19"),
    ):
        await runner._check_and_trigger()
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(5, "2026-03-20"),
    ):
        await runner._check_and_trigger()
    assert sync_mock.await_count == 2


@pytest.mark.asyncio
async def test_metrics_updated_on_success():
    runner, _ = _make_runner(close_hour=5)
    assert runner._eod_recon_status._value.get() == _STATUS_PENDING
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(5, "2026-03-19"),
    ):
        await runner._check_and_trigger()
    assert runner._eod_recon_status._value.get() == _STATUS_SUCCESS
    assert runner._eod_recon_last_ts._value.get() > 0


@pytest.mark.asyncio
async def test_metrics_updated_on_failure():
    runner, _ = _make_runner(
        close_hour=5,
        sync_side_effect=RuntimeError("broker down"),
    )
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(5, "2026-03-19"),
    ):
        await runner._check_and_trigger()
    assert runner._eod_recon_status._value.get() == _STATUS_FAILURE
    assert runner._eod_recon_last_ts._value.get() > 0


@pytest.mark.asyncio
async def test_configurable_hour():
    runner, sync_mock = _make_runner(close_hour=14)
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(5, "2026-03-19"),
    ):
        await runner._check_and_trigger()
    sync_mock.assert_not_awaited()
    with patch(
        "hft_platform.execution.eod_recon._current_utc_hour_and_date",
        return_value=(14, "2026-03-19"),
    ):
        await runner._check_and_trigger()
    sync_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_loop_stops():
    runner, _ = _make_runner(close_hour=5)

    async def _stop_soon():
        await asyncio.sleep(0.05)
        runner.stop()

    with patch("hft_platform.execution.eod_recon._POLL_INTERVAL_S", 0.02):
        await asyncio.wait_for(
            asyncio.gather(runner.run(), _stop_soon()),
            timeout=5.0,
        )
    assert not runner.running


@pytest.mark.asyncio
async def test_env_var_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HFT_EOD_CLOSE_HOUR_UTC", "13")
    recon = MagicMock()
    recon.sync_portfolio = AsyncMock()
    runner = EODReconciliationRunner(recon_service=recon)
    assert runner.close_hour_utc == 13
