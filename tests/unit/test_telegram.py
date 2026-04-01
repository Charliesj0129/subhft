"""Unit tests for async Telegram sender and command poller."""

from __future__ import annotations

import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Build a minimal aiohttp stub and inject into sys.modules so the telegram
# module can be imported without the real aiohttp dependency.
# ---------------------------------------------------------------------------


def _build_aiohttp_stub() -> types.ModuleType:
    """Return a minimal aiohttp stub module with ClientSession."""
    stub = types.ModuleType("aiohttp")

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

    stub.ClientSession = _FakeClientSession  # type: ignore[attr-defined]
    return stub


# Inject before any telegram import happens
if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = _build_aiohttp_stub()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sender(enabled: bool = True, rate_limit_seconds: float = 1.0):
    from hft_platform.notifications.telegram import TelegramSender

    return TelegramSender(
        bot_token="test-token",
        chat_id="123456",
        enabled=enabled,
        rate_limit_seconds=rate_limit_seconds,
    )


def _make_update(
    update_id: int = 1,
    from_id: int = 123456,
    text: str = "/status",
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": from_id},
            "text": text,
        },
    }


def _make_mock_session(status: int = 200, post_ok: bool = True) -> tuple[MagicMock, MagicMock]:
    """Build a mock aiohttp.ClientSession with configurable POST response."""
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.close = AsyncMock()  # async close must be awaitable
    return mock_session, mock_response


# ---------------------------------------------------------------------------
# TelegramSender tests
# ---------------------------------------------------------------------------


class TestTelegramSender:
    @pytest.mark.asyncio
    async def test_send_message_posts_to_telegram_api(self):
        """A successful send should POST to the correct Telegram API URL."""
        sender = _make_sender()
        mock_session, mock_response = _make_mock_session()

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = mock_session
            # Inject the session directly to bypass lazy creation
            sender._session = mock_session
            result = await sender.send("Hello HFT")

        assert result is True
        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert "sendMessage" in call_kwargs[0][0]
        payload = call_kwargs[1]["json"]
        assert payload["chat_id"] == "123456"
        assert payload["text"] == "Hello HFT"
        assert payload["parse_mode"] == "HTML"

    @pytest.mark.asyncio
    async def test_send_when_disabled_is_noop(self):
        """When enabled=False, send() must return False without any HTTP call."""
        sender = _make_sender(enabled=False)

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            result = await sender.send("should not send")

        assert result is False
        mock_aiohttp.ClientSession.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_failure_returns_false_no_raise(self):
        """Network exception should be swallowed and return False."""
        sender = _make_sender()
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=Exception("connection refused"))

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = mock_session
            sender._session = mock_session
            result = await sender.send("boom")

        assert result is False

    @pytest.mark.asyncio
    async def test_rate_limiter_batches_non_critical(self):
        """Non-critical send within rate-limit window should return False."""
        sender = _make_sender(rate_limit_seconds=60.0)
        sender._last_send_ts = time.monotonic()  # simulate recent send

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            result = await sender.send("rate limited message")

        assert result is False
        mock_aiohttp.ClientSession.assert_not_called()

    @pytest.mark.asyncio
    async def test_critical_bypasses_rate_limit(self):
        """Critical=True should bypass the rate limit and send immediately."""
        sender = _make_sender(rate_limit_seconds=60.0)
        sender._last_send_ts = time.monotonic()  # simulate recent send

        mock_session, _ = _make_mock_session()

        with patch("hft_platform.notifications.telegram.aiohttp"):
            sender._session = mock_session
            result = await sender.send("CRITICAL ALERT", critical=True)

        assert result is True

    @pytest.mark.asyncio
    async def test_session_is_reused(self):
        """Two consecutive sends should reuse the same aiohttp session."""
        sender = _make_sender(rate_limit_seconds=0.0)

        mock_session, _ = _make_mock_session()

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = mock_session
            await sender.send("first")
            await sender.send("second")

        # ClientSession constructor should only be called once (lazily on first send)
        assert mock_aiohttp.ClientSession.call_count == 1
        assert mock_session.post.call_count == 2

        await sender.close()


# ---------------------------------------------------------------------------
# TelegramCommandPoller tests
# ---------------------------------------------------------------------------


class TestTelegramCommandPoller:
    def _make_poller(self, redis_client=None):
        from hft_platform.notifications.telegram import TelegramCommandPoller

        redis = redis_client or MagicMock()
        return TelegramCommandPoller(
            bot_token="test-token",
            chat_id="123456",
            redis_client=redis,
            poll_interval=5.0,
        )

    def _mock_get_session(self, update_data: dict) -> tuple[MagicMock, MagicMock]:
        """Build a mock session whose GET returns the given update_data."""
        mock_get_response = AsyncMock()
        mock_get_response.status = 200
        mock_get_response.json = AsyncMock(return_value=update_data)
        mock_get_response.__aenter__ = AsyncMock(return_value=mock_get_response)
        mock_get_response.__aexit__ = AsyncMock(return_value=False)

        mock_post_response = AsyncMock()
        mock_post_response.__aenter__ = AsyncMock(return_value=mock_post_response)
        mock_post_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_get_response)
        mock_session.post = MagicMock(return_value=mock_post_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        return mock_session, mock_get_response

    @pytest.mark.asyncio
    async def test_command_poller_stop_sets_redis_key(self):
        """/stop command from whitelisted chat_id should set emergency halt key."""
        redis_mock = AsyncMock()
        poller = self._make_poller(redis_client=redis_mock)

        update_data = {"result": [_make_update(update_id=1, from_id=123456, text="/stop")]}
        mock_session, _ = self._mock_get_session(update_data)

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = mock_session
            mock_aiohttp.ClientTimeout = MagicMock()
            await poller.poll_once()

        redis_mock.set.assert_awaited_once_with("hft:emergency_halt", "1")
        assert poller._offset == 2

    @pytest.mark.asyncio
    async def test_command_poller_ignores_wrong_chat_id(self):
        """Messages from a different chat_id should be silently ignored."""
        redis_mock = AsyncMock()
        poller = self._make_poller(redis_client=redis_mock)

        # from_id differs from whitelisted 123456
        update_data = {"result": [_make_update(update_id=5, from_id=999999, text="/stop")]}
        mock_session, _ = self._mock_get_session(update_data)

        with patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession.return_value = mock_session
            mock_aiohttp.ClientTimeout = MagicMock()
            await poller.poll_once()

        redis_mock.set.assert_not_awaited()
        # Offset still advances so we don't re-process the update
        assert poller._offset == 6
