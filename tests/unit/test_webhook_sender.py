"""Tests for WebhookSender — secondary notification channel."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

WEBHOOK_URL = "https://hooks.example.com/test-channel"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sender():
    from hft_platform.notifications.webhook import WebhookSender

    return WebhookSender(url=WEBHOOK_URL, timeout=5.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_send_posts_json(sender) -> None:
    """Verify POST sends JSON payload {"content": text} to the configured URL."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_client_session_cls = MagicMock(return_value=mock_session)
    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = mock_client_session_cls
    mock_aiohttp.ClientTimeout = MagicMock()

    # Patch the import itself — webhook.py does `import aiohttp` lazily
    with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
        result = await sender.send("HALT: risk limit breached")

    assert result is True
    mock_session.post.assert_called_once()
    call_args = mock_session.post.call_args
    assert call_args.args[0] == WEBHOOK_URL
    assert call_args.kwargs["json"] == {"content": "HALT: risk limit breached"}


@pytest.mark.asyncio
async def test_webhook_send_failure_returns_false(sender) -> None:
    """Connection error returns False without crashing."""
    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(side_effect=ConnectionError("refused"))
    mock_aiohttp.ClientTimeout = MagicMock()

    with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
        result = await sender.send("test message")

    assert result is False


@pytest.mark.asyncio
async def test_webhook_empty_url_send_returns_false() -> None:
    """Empty URL short-circuits to False without attempting HTTP."""
    from hft_platform.notifications.webhook import WebhookSender

    ws = WebhookSender(url="", timeout=1.0)
    result = await ws.send("should not send")
    assert result is False


@pytest.mark.asyncio
async def test_dispatcher_critical_calls_fallback() -> None:
    """notify_halt sends to both primary and fallback."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary = AsyncMock()
    primary.send = AsyncMock(return_value=True)
    fallback = AsyncMock()
    fallback.send = AsyncMock(return_value=True)

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=fallback)
    await dispatcher.notify_halt(reason="test halt")

    primary.send.assert_called_once()
    fallback.send.assert_called_once()


@pytest.mark.asyncio
async def test_dispatcher_daily_loss_calls_fallback() -> None:
    """notify_daily_loss sends to both primary and fallback."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary = AsyncMock()
    primary.send = AsyncMock(return_value=True)
    fallback = AsyncMock()
    fallback.send = AsyncMock(return_value=True)

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=fallback)
    await dispatcher.notify_daily_loss(pnl_ntd=-50000, limit_ntd=-100000)

    primary.send.assert_called_once()
    fallback.send.assert_called_once()


@pytest.mark.asyncio
async def test_dispatcher_no_fallback_still_works() -> None:
    """Dispatcher works normally without fallback configured."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary = AsyncMock()
    primary.send = AsyncMock(return_value=True)

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=None)
    await dispatcher.notify_halt(reason="test")

    primary.send.assert_called_once()
