"""
Tests verifying the ExecutionGateway double-start fix.

Root cause: ExecutionGateway.run() previously called adapter.run(), creating
two concurrent consumers on the same order_queue / _api_queue.  The fix turns
ExecutionGateway.run() into a heartbeat-only monitor loop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.gateway import ExecutionGateway


def _make_adapter(running: bool = True) -> MagicMock:
    """Return a mock OrderAdapter with the minimal surface used by gateway."""
    adapter = MagicMock()
    adapter.running = running
    adapter.run = AsyncMock()
    return adapter


def _make_metrics() -> MagicMock:
    metric = MagicMock()
    metric.set = MagicMock()
    metric.inc = MagicMock()
    return metric


def _make_gateway(adapter: MagicMock | None = None) -> tuple[ExecutionGateway, MagicMock]:
    if adapter is None:
        adapter = _make_adapter()
    metrics = MagicMock()
    metrics.execution_gateway_alive = _make_metrics()
    metrics.execution_gateway_heartbeat_ts = _make_metrics()
    metrics.execution_gateway_errors_total = _make_metrics()

    with patch(
        "hft_platform.execution.gateway.MetricsRegistry.get",
        return_value=metrics,
    ):
        gw = ExecutionGateway(adapter)
    return gw, metrics


@pytest.mark.asyncio
async def test_run_does_not_call_adapter_run() -> None:
    """ExecutionGateway.run() must be a heartbeat loop, not a delegate to adapter.run()."""
    adapter = _make_adapter(running=True)
    gw, _ = _make_gateway(adapter)

    async def _stop_after_one_tick() -> None:
        await asyncio.sleep(0)
        gw.running = False

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.return_value = None
        call_count = 0

        async def _sleep_side_effect(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                gw.running = False

        mock_sleep.side_effect = _sleep_side_effect
        await gw.run()

    adapter.run.assert_not_called()


@pytest.mark.asyncio
async def test_run_updates_heartbeat_metric() -> None:
    """run() must call execution_gateway_heartbeat_ts.set() on every iteration."""
    adapter = _make_adapter(running=True)
    gw, metrics = _make_gateway(adapter)

    iteration_count = 0

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        async def _sleep_side_effect(delay: float) -> None:
            nonlocal iteration_count
            iteration_count += 1
            if iteration_count >= 2:
                gw.running = False

        mock_sleep.side_effect = _sleep_side_effect
        await gw.run()

    assert metrics.execution_gateway_heartbeat_ts.set.call_count >= 2


@pytest.mark.asyncio
async def test_run_sets_alive_metric() -> None:
    adapter = _make_adapter(running=True)
    gw, metrics = _make_gateway(adapter)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        async def _sleep_side_effect(delay: float) -> None:
            gw.running = False

        mock_sleep.side_effect = _sleep_side_effect
        await gw.run()

    calls = [c.args[0] for c in metrics.execution_gateway_alive.set.call_args_list]
    assert 1 in calls, "expected alive=1 set at start"
    assert 0 in calls, "expected alive=0 set at exit"
    assert calls[-1] == 0, "last call must set alive=0 (cleanup)"


@pytest.mark.asyncio
async def test_run_exits_when_adapter_stops() -> None:
    """Heartbeat loop must exit if adapter.running goes False even if gw.running is True."""
    adapter = _make_adapter(running=True)
    gw, _ = _make_gateway(adapter)

    call_count = 0

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        async def _sleep_side_effect(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            adapter.running = False

        mock_sleep.side_effect = _sleep_side_effect
        await gw.run()

    assert call_count == 1
    assert gw.running is True


def test_stop_sets_running_false_on_both() -> None:
    adapter = _make_adapter(running=True)
    gw, _ = _make_gateway(adapter)
    gw.running = True

    gw.stop()

    assert gw.running is False
    assert adapter.running is False


@pytest.mark.asyncio
async def test_run_exits_immediately_if_stopped_before_start() -> None:
    """If stop() is called before run(), the loop should not iterate."""
    adapter = _make_adapter(running=False)
    gw, _ = _make_gateway(adapter)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await gw.run()
        mock_sleep.assert_not_called()
