"""Tests for WebhookSender — secondary notification channel."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Build a minimal aiohttp stub so webhook module can be imported without
# the real aiohttp dependency (same pattern as test_telegram.py).
# ---------------------------------------------------------------------------


def _build_aiohttp_stub() -> types.ModuleType:
    """Return a minimal aiohttp stub module with ClientSession + ClientTimeout."""
    stub = types.ModuleType("aiohttp")

    class _FakeClientSession:
        def __init__(self, *a, **kw):
            pass

    class _FakeClientTimeout:
        def __init__(self, *a, **kw):
            pass

    stub.ClientSession = _FakeClientSession  # type: ignore[attr-defined]
    stub.ClientTimeout = _FakeClientTimeout  # type: ignore[attr-defined]
    return stub


if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = _build_aiohttp_stub()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WEBHOOK_URL = "https://hooks.example.com/test-channel"


@pytest.fixture
def sender():
    from hft_platform.notifications.webhook import WebhookSender

    return WebhookSender(url=WEBHOOK_URL, timeout=5.0)


# ------------------------------------------------------------------
# test_webhook_send_posts_json
# ------------------------------------------------------------------


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

    # Patch both the module-level and the lazy-import path to handle
    # test ordering where real aiohttp may already be imported
    with patch.dict("sys.modules", {"aiohttp": sys.modules.get("aiohttp", _build_aiohttp_stub())}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await sender.send("HALT: risk limit breached")

    assert result is True
    mock_session.post.assert_called_once()
    call_args = mock_session.post.call_args
    assert call_args.args[0] == WEBHOOK_URL
    assert call_args.kwargs["json"] == {"content": "HALT: risk limit breached"}


# ------------------------------------------------------------------
# test_webhook_send_failure_returns_false
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_send_failure_returns_false(sender) -> None:
    """Connection error returns False without crashing."""
    with patch("aiohttp.ClientSession", side_effect=ConnectionError("refused")):
        result = await sender.send("test message")

    assert result is False


# ------------------------------------------------------------------
# test_webhook_disabled_when_no_url
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_empty_url_send_returns_false() -> None:
    """WebhookSender with empty URL should short-circuit and return False."""
    from hft_platform.notifications.webhook import WebhookSender

    ws = WebhookSender(url="", timeout=5.0)
    result = await ws.send("test")
    assert result is False


# ------------------------------------------------------------------
# test_dispatcher_critical_calls_fallback
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_critical_calls_fallback() -> None:
    """notify_halt sends to both primary sender and fallback webhook."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary = AsyncMock()
    primary.send = AsyncMock(return_value=True)

    fallback = AsyncMock()
    fallback.send = AsyncMock(return_value=True)

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=fallback)
    await dispatcher.notify_halt(reason="circuit breaker tripped")

    # Primary must be called with critical=True
    primary.send.assert_awaited_once()
    assert primary.send.call_args.kwargs["critical"] is True

    # Fallback must also be called
    fallback.send.assert_awaited_once()
    msg = fallback.send.call_args.args[0]
    assert "HALT" in msg


@pytest.mark.asyncio
async def test_dispatcher_daily_loss_calls_fallback() -> None:
    """notify_daily_loss sends to both primary and fallback."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary = AsyncMock()
    primary.send = AsyncMock(return_value=True)

    fallback = AsyncMock()
    fallback.send = AsyncMock(return_value=True)

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=fallback)
    await dispatcher.notify_daily_loss(pnl_ntd=-60_000, limit_ntd=-50_000)

    primary.send.assert_awaited_once()
    fallback.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_no_fallback_still_works() -> None:
    """Dispatcher without fallback_sender works as before."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary = AsyncMock()
    primary.send = AsyncMock(return_value=True)

    dispatcher = NotificationDispatcher(sender=primary)
    await dispatcher.notify_halt(reason="test")

    primary.send.assert_awaited_once()
