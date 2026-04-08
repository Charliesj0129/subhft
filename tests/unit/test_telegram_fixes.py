"""Tests for T1/T2/T8 fixes in the Telegram notifications module.

T1: TelegramSender POST uses aiohttp.ClientTimeout(total=10)
T2: TelegramCommandPoller reuses its aiohttp session across poll_once() calls
T8: TelegramCommandPoller awaits redis.set() for async redis clients
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal aiohttp stub (same approach as test_telegram.py)
# ---------------------------------------------------------------------------


def _build_aiohttp_stub() -> types.ModuleType:
    stub = types.ModuleType("aiohttp")

    class _FakeClientTimeout:
        def __init__(self, *args, **kwargs):
            self.total = kwargs.get("total")

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

    stub.ClientSession = _FakeClientSession  # type: ignore[attr-defined]
    stub.ClientTimeout = _FakeClientTimeout  # type: ignore[attr-defined]
    return stub


if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = _build_aiohttp_stub()
elif not hasattr(sys.modules["aiohttp"], "ClientTimeout"):
    # Another test's stub may lack ClientTimeout — patch it in
    class _FakeClientTimeout:
        def __init__(self, *args, **kwargs):
            self.total = kwargs.get("total")

    sys.modules["aiohttp"].ClientTimeout = _FakeClientTimeout  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(
    update_id: int = 1,
    from_id: int = 123456,
    text: str = "/status",
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": from_id},
            "chat": {"id": from_id},
            "text": text,
        },
    }


def _make_mock_get_session(update_data: dict) -> MagicMock:
    """Return a mock aiohttp.ClientSession for GET requests in the poller."""
    mock_get_response = AsyncMock()
    mock_get_response.status = 200
    mock_get_response.json = AsyncMock(return_value=update_data)
    mock_get_response.__aenter__ = AsyncMock(return_value=mock_get_response)
    mock_get_response.__aexit__ = AsyncMock(return_value=False)

    mock_post_response = AsyncMock()
    mock_post_response.__aenter__ = AsyncMock(return_value=mock_post_response)
    mock_post_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.get = MagicMock(return_value=mock_get_response)
    mock_session.post = MagicMock(return_value=mock_post_response)
    return mock_session


def _make_sender(enabled: bool = True, rate_limit_seconds: float = 0.0):
    from hft_platform.notifications.telegram import TelegramSender

    return TelegramSender(
        bot_token="test-token",
        chat_id="123456",
        enabled=enabled,
        rate_limit_seconds=rate_limit_seconds,
    )


def _make_poller(redis_client=None):
    from hft_platform.notifications.telegram import TelegramCommandPoller

    redis = redis_client or MagicMock()
    return TelegramCommandPoller(
        bot_token="test-token",
        chat_id="123456",
        redis_client=redis,
        poll_interval=5.0,
    )


# ---------------------------------------------------------------------------
# T1: TelegramSender POST timeout
# ---------------------------------------------------------------------------


class TestT1SenderTimeout:
    @pytest.mark.asyncio
    async def test_post_includes_client_timeout(self):
        """TelegramSender.send() must pass aiohttp.ClientTimeout(total=10) to POST."""
        sender = _make_sender()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_response)

        captured_timeout = None

        def _capture_post(url, **kwargs):
            nonlocal captured_timeout
            captured_timeout = kwargs.get("timeout")
            return mock_response

        mock_session.post = MagicMock(side_effect=_capture_post)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session
        mock_aiohttp.ClientTimeout = sys.modules["aiohttp"].ClientTimeout  # type: ignore[attr-defined]

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            sender._session = mock_session
            result = await sender.send("test alert")

        assert result is True
        assert captured_timeout is not None, "timeout kwarg was not passed to session.post()"
        assert captured_timeout.total == 10, f"Expected ClientTimeout(total=10), got total={captured_timeout.total}"

    @pytest.mark.asyncio
    async def test_post_timeout_kwarg_present_on_failed_send(self):
        """Timeout is passed even when the server returns a non-200 status."""
        sender = _make_sender()

        mock_response = AsyncMock()
        mock_response.status = 429
        mock_response.text = AsyncMock(return_value="Too Many Requests")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False

        captured_timeout = None

        def _capture_post(url, **kwargs):
            nonlocal captured_timeout
            captured_timeout = kwargs.get("timeout")
            return mock_response

        mock_session.post = MagicMock(side_effect=_capture_post)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session
        mock_aiohttp.ClientTimeout = sys.modules["aiohttp"].ClientTimeout  # type: ignore[attr-defined]

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            sender._session = mock_session
            result = await sender.send("test alert")

        assert result is False
        assert captured_timeout is not None
        assert captured_timeout.total == 10


# ---------------------------------------------------------------------------
# T2: TelegramCommandPoller session reuse
# ---------------------------------------------------------------------------


class TestT2PollerSessionReuse:
    @pytest.mark.asyncio
    async def test_session_created_once_across_poll_calls(self):
        """poll_once() must reuse self._session; ClientSession ctor called only once."""
        update_data = {"result": []}
        mock_session = _make_mock_get_session(update_data)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session
        mock_aiohttp.ClientTimeout = sys.modules["aiohttp"].ClientTimeout  # type: ignore[attr-defined]

        poller = _make_poller()

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            await poller.poll_once()
            await poller.poll_once()
            await poller.poll_once()

        # Session constructor called exactly once — session is reused
        assert mock_aiohttp.ClientSession.call_count == 1

    @pytest.mark.asyncio
    async def test_session_stored_as_attribute(self):
        """After poll_once(), self._session must be set (not None)."""
        update_data = {"result": []}
        mock_session = _make_mock_get_session(update_data)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session

        poller = _make_poller()
        assert poller._session is None  # starts as None

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            await poller.poll_once()

        assert poller._session is mock_session

    @pytest.mark.asyncio
    async def test_session_recreated_when_closed(self):
        """If the stored session is closed, a new one is created on next poll."""
        update_data = {"result": []}
        mock_session_1 = _make_mock_get_session(update_data)
        mock_session_2 = _make_mock_get_session(update_data)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.side_effect = [mock_session_1, mock_session_2]

        poller = _make_poller()

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            await poller.poll_once()
            assert poller._session is mock_session_1

            # Simulate session being closed externally
            mock_session_1.closed = True

            await poller.poll_once()
            assert poller._session is mock_session_2

        assert mock_aiohttp.ClientSession.call_count == 2

    @pytest.mark.asyncio
    async def test_close_cleans_up_session(self):
        """close() must await session.close() and set _session to None."""
        poller = _make_poller()

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        poller._session = mock_session

        await poller.close()

        mock_session.close.assert_awaited_once()
        assert poller._session is None

    @pytest.mark.asyncio
    async def test_close_is_safe_when_session_already_closed(self):
        """close() on an already-closed session must not raise."""
        poller = _make_poller()

        mock_session = MagicMock()
        mock_session.closed = True
        mock_session.close = AsyncMock()
        poller._session = mock_session

        await poller.close()  # should not raise

        mock_session.close.assert_not_awaited()
        assert poller._session is None

    @pytest.mark.asyncio
    async def test_close_is_safe_when_no_session(self):
        """close() with _session=None must not raise."""
        poller = _make_poller()
        assert poller._session is None
        await poller.close()  # must not raise
        assert poller._session is None


# ---------------------------------------------------------------------------
# T8: TelegramCommandPoller awaits redis.set()
# ---------------------------------------------------------------------------


class TestT8RedisAwait:
    @pytest.mark.asyncio
    async def test_async_redis_set_is_awaited(self):
        """An async redis client's set() coroutine must be awaited on /stop command."""
        redis_mock = AsyncMock()  # async redis — set() returns a coroutine
        poller = _make_poller(redis_client=redis_mock)

        update_data = {"result": [_make_update(update_id=1, from_id=123456, text="/stop")]}
        mock_session = _make_mock_get_session(update_data)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            await poller.poll_once()

        # AsyncMock tracks awaits separately from calls
        redis_mock.set.assert_awaited_once_with("hft:emergency_halt", "1")

    @pytest.mark.asyncio
    async def test_async_redis_set_called_with_correct_args(self):
        """redis.set() must receive ('hft:emergency_halt', '1') on /stop."""
        redis_mock = AsyncMock()
        poller = _make_poller(redis_client=redis_mock)

        update_data = {"result": [_make_update(update_id=3, from_id=123456, text="/stop")]}
        mock_session = _make_mock_get_session(update_data)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            await poller.poll_once()

        call_args = redis_mock.set.call_args
        assert call_args[0] == ("hft:emergency_halt", "1")

    @pytest.mark.asyncio
    async def test_redis_set_not_called_for_status_command(self):
        """/status command must not trigger redis.set()."""
        redis_mock = AsyncMock()
        poller = _make_poller(redis_client=redis_mock)

        update_data = {"result": [_make_update(update_id=7, from_id=123456, text="/status")]}
        mock_session = _make_mock_get_session(update_data)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            await poller.poll_once()

        redis_mock.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_redis_set_not_called_for_wrong_sender(self):
        """redis.set() must not be called for /stop from an unknown chat_id."""
        redis_mock = AsyncMock()
        poller = _make_poller(redis_client=redis_mock)

        update_data = {"result": [_make_update(update_id=9, from_id=999999, text="/stop")]}
        mock_session = _make_mock_get_session(update_data)

        mock_aiohttp = MagicMock()
        mock_aiohttp.ClientSession.return_value = mock_session

        with patch("hft_platform.notifications.telegram.aiohttp", mock_aiohttp):
            await poller.poll_once()

        redis_mock.set.assert_not_called()
