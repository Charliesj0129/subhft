"""Tests for notification event dispatcher."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    sender.enabled = True
    return sender


@pytest.fixture
def dispatcher(mock_sender: AsyncMock):
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    return NotificationDispatcher(sender=mock_sender)


@pytest.mark.asyncio
async def test_notify_halt_sends_critical(dispatcher, mock_sender) -> None:
    await dispatcher.notify_halt(reason="risk limit breached")

    mock_sender.send.assert_awaited_once()
    call_kwargs = mock_sender.send.call_args
    assert call_kwargs.kwargs["critical"] is True
    assert "HALT" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_notify_daily_loss_sends_critical(dispatcher, mock_sender) -> None:
    await dispatcher.notify_daily_loss(pnl_ntd=-50_000, limit_ntd=-40_000)

    mock_sender.send.assert_awaited_once()
    call_kwargs = mock_sender.send.call_args
    assert call_kwargs.kwargs["critical"] is True
    # PnL value should appear in message
    assert "-50,000" in call_kwargs.args[0] or "-50000" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_notify_stormguard_change_sends_warning(dispatcher, mock_sender) -> None:
    await dispatcher.notify_stormguard_change(old="NORMAL", new="CAUTION", reason="high volatility")

    mock_sender.send.assert_awaited_once()
    call_kwargs = mock_sender.send.call_args
    assert call_kwargs.kwargs["critical"] is False


@pytest.mark.asyncio
async def test_notify_pre_market_pass(dispatcher, mock_sender) -> None:
    await dispatcher.notify_pre_market_pass()

    mock_sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_pre_market_fail(dispatcher, mock_sender) -> None:
    failed = ["ClickHouse unreachable", "Redis timeout"]
    await dispatcher.notify_pre_market_fail(failed_checks=failed)

    mock_sender.send.assert_awaited_once()
    message = mock_sender.send.call_args.args[0]
    assert "ClickHouse unreachable" in message
    assert "Redis timeout" in message


@pytest.mark.asyncio
async def test_notify_reconciliation_mismatch(dispatcher, mock_sender) -> None:
    await dispatcher.notify_reconciliation_mismatch(
        platform_pnl=12_000,
        broker_pnl=11_500,
        ch_pnl=12_100,
    )

    mock_sender.send.assert_awaited_once()
    message = mock_sender.send.call_args.args[0]
    assert "12,000" in message or "12000" in message
    assert "11,500" in message or "11500" in message


@pytest.mark.asyncio
async def test_notify_reconnect(dispatcher, mock_sender) -> None:
    await dispatcher.notify_reconnect(count=3, flap_status="OK")

    mock_sender.send.assert_awaited_once()
    message = mock_sender.send.call_args.args[0]
    assert "3" in message


@pytest.mark.asyncio
async def test_notify_process_restart(dispatcher, mock_sender) -> None:
    await dispatcher.notify_process_restart(attempt=2, max_attempts=5)

    mock_sender.send.assert_awaited_once()
    message = mock_sender.send.call_args.args[0]
    assert "2" in message
    assert "5" in message


@pytest.mark.asyncio
async def test_notify_daily_report(dispatcher, mock_sender) -> None:
    await dispatcher.notify_daily_report(
        date_str="2026-03-23",
        pnl_ntd=8_500,
        buys=12,
        sells=12,
        fills=24,
        position_status="FLAT",
        reconciliation_status="OK",
        latency_p95_ms=1.23,
        reconnect_count=0,
        storm_guard_state="NORMAL",
        memory_gb=0.8,
        memory_max_gb=1.1,
    )

    mock_sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_weekly_summary(dispatcher, mock_sender) -> None:
    await dispatcher.notify_weekly_summary(
        week_label="2026-W12",
        date_range="2026-03-16 ~ 2026-03-20",
        total_pnl_ntd=42_000,
        trading_days=5,
        avg_trades=20.4,
        best_day_ntd=15_000,
        worst_day_ntd=-2_000,
        reconciliation_match=True,
        halt_count=0,
        reconnect_count=1,
        latency_p95_avg_ms=1.05,
        rss_peak_gb=1.2,
        uptime_pct=99.8,
    )

    mock_sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_with_disabled_sender(mock_sender) -> None:
    """Dispatcher always calls send; no exception raised even if sender is disabled."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    mock_sender.enabled = False
    mock_sender.send = AsyncMock(return_value=False)
    d = NotificationDispatcher(sender=mock_sender)

    # Should not raise, send still called
    await d.notify_halt(reason="test")
    mock_sender.send.assert_awaited_once()
