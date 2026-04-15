"""Tests for the AlertRouter core routing pipeline."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule


def _make_alert(
    *,
    alert_id: str = "a-001",
    severity: AlertSeverity = AlertSeverity.WARN,
    category: str = "feed",
    dedup_key: str | None = None,
    ts_ns: int = 1_000_000_000_000_000_000,
) -> Alert:
    return Alert(
        alert_id=alert_id, severity=severity, category=category, source="test",
        title="Test alert", detail="Test detail", ts_ns=ts_ns,
        dedup_key=dedup_key, metadata=None,
    )


@pytest.fixture
def mock_telegram() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def mock_webhook() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def router(mock_telegram, mock_webhook):
    from hft_platform.notifications.alert_router import AlertRouter
    return AlertRouter(telegram_sender=mock_telegram, webhook_sender=mock_webhook)


@pytest.mark.asyncio
async def test_warn_sends_telegram_only(router, mock_telegram, mock_webhook):
    alert = _make_alert(severity=AlertSeverity.WARN)
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()
    mock_webhook.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_critical_sends_telegram_and_webhook(router, mock_telegram, mock_webhook):
    alert = _make_alert(severity=AlertSeverity.CRITICAL)
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()
    mock_webhook.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_fatal_sends_telegram_and_webhook(router, mock_telegram, mock_webhook):
    alert = _make_alert(severity=AlertSeverity.FATAL)
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()
    mock_webhook.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_silenced_alert_not_sent(router, mock_telegram):
    rule = SilenceRule(
        rule_id="s-001", category="feed", source=None,
        severity_max=AlertSeverity.WARN, start_ns=0, end_ns=0, reason="test silence",
    )
    router.add_silence(rule)
    alert = _make_alert(severity=AlertSeverity.WARN, category="feed")
    await router.emit(alert)
    mock_telegram.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_silence_allows_sending(router, mock_telegram):
    rule = SilenceRule(
        rule_id="s-001", category="feed", source=None,
        severity_max=AlertSeverity.WARN, start_ns=0, end_ns=0, reason="test silence",
    )
    router.add_silence(rule)
    router.remove_silence("s-001")
    alert = _make_alert(severity=AlertSeverity.WARN, category="feed")
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dedup_suppresses_duplicate(router, mock_telegram):
    a1 = _make_alert(dedup_key="dup_key", ts_ns=1_000_000_000_000_000_000)
    a2 = _make_alert(dedup_key="dup_key", ts_ns=1_000_000_000_000_000_000 + 1_000_000_000, alert_id="a-002")
    await router.emit(a1)
    await router.emit(a2)
    assert mock_telegram.send.await_count == 1


@pytest.mark.asyncio
async def test_info_batched_not_immediate(router, mock_telegram):
    alert = _make_alert(severity=AlertSeverity.INFO)
    await router.emit(alert)
    mock_telegram.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_info_batch(router, mock_telegram):
    a1 = _make_alert(severity=AlertSeverity.INFO, alert_id="i-001")
    a2 = _make_alert(severity=AlertSeverity.INFO, alert_id="i-002")
    await router.emit(a1)
    await router.emit(a2)
    await router.flush_info_batch()
    mock_telegram.send.assert_awaited_once()
    msg = mock_telegram.send.call_args.args[0]
    assert "2" in msg


def test_active_alerts_returns_unacked(router):
    a1 = _make_alert(severity=AlertSeverity.CRITICAL, alert_id="c-001")
    a2 = _make_alert(severity=AlertSeverity.FATAL, alert_id="f-001")
    router._escalation.track(a1)
    router._escalation.track(a2)
    active = router.active_alerts()
    assert len(active) == 2
