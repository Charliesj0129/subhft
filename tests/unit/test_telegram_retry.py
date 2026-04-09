"""Unit tests for TelegramSender retry logic on critical messages (P2-2)."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Inject a minimal aiohttp stub so the module can be imported without the
# real dependency.  Mirror the approach in test_telegram.py.
# ---------------------------------------------------------------------------


def _build_aiohttp_stub() -> types.ModuleType:
    stub = types.ModuleType("aiohttp")

    class _FakeClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

    stub.ClientSession = _FakeClientSession  # type: ignore[attr-defined]
    stub.ClientTimeout = _FakeClientTimeout  # type: ignore[attr-defined]
    return stub


if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = _build_aiohttp_stub()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sender(rate_limit_seconds: float = 0.0):
    from hft_platform.notifications.telegram import TelegramSender

    return TelegramSender(
        bot_token="test-token",
        chat_id="123456",
        enabled=True,
        rate_limit_seconds=rate_limit_seconds,
    )


def _make_response(status: int, body: str = "") -> AsyncMock:
    """Build an async context-manager response mock."""
    mock = AsyncMock()
    mock.status = status
    mock.text = AsyncMock(return_value=body)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _make_session(*responses) -> MagicMock:
    """Build a mock session whose post() returns responses in sequence."""
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=list(responses))
    session.close = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCriticalMessageRetry:
    """Critical messages should retry on transient errors."""

    @pytest.mark.asyncio
    async def test_critical_retries_on_500_succeeds_on_second_attempt(self):
        """500 on first attempt → retry → 200 on second → returns True."""
        sender = _make_sender()
        resp_500 = _make_response(500)
        resp_200 = _make_response(200)
        session = _make_session(resp_500, resp_200)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("HALT", critical=True)

        assert result is True
        assert session.post.call_count == 2
        mock_sleep.assert_awaited_once_with(1.0)  # backoff: 1.0 * 2**0

    @pytest.mark.asyncio
    async def test_critical_retries_on_429_succeeds_on_second_attempt(self):
        """429 (rate-limit) is a transient status; should retry."""
        sender = _make_sender()
        resp_429 = _make_response(429)
        resp_200 = _make_response(200)
        session = _make_session(resp_429, resp_200)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("daily_loss_alert", critical=True)

        assert result is True
        mock_sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_critical_retries_on_network_exception_succeeds(self):
        """Network exception on first attempt → retry → 200 on second → True."""
        sender = _make_sender()
        resp_200 = _make_response(200)
        session = MagicMock()
        session.closed = False
        session.post = MagicMock(side_effect=[ConnectionError("network blip"), resp_200])

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("CRITICAL", critical=True)

        assert result is True
        assert session.post.call_count == 2
        mock_sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_critical_exhausts_all_retries_returns_false(self):
        """If every attempt fails, returns False after MAX_CRITICAL_RETRIES+1 tries."""
        sender = _make_sender()
        # 3 total attempts (initial + 2 retries), all 503
        resp_503a = _make_response(503)
        resp_503b = _make_response(503)
        resp_503c = _make_response(503)
        session = _make_session(resp_503a, resp_503b, resp_503c)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("HALT", critical=True)

        assert result is False
        # 3 total POST calls (attempt 0, 1, 2)
        assert session.post.call_count == 3
        # 2 sleeps between attempts (after attempt 0 and after attempt 1)
        assert mock_sleep.await_count == 2
        # Exponential backoff: 1.0 * 2**0 = 1.0, 1.0 * 2**1 = 2.0
        mock_sleep.assert_has_awaits([call(1.0), call(2.0)])

    @pytest.mark.asyncio
    async def test_critical_stops_retrying_on_permanent_4xx(self):
        """400 Bad Request is a permanent error; no retry should happen."""
        sender = _make_sender()
        resp_400 = _make_response(400, body="Bad Request")
        session = _make_session(resp_400)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("HALT", critical=True)

        assert result is False
        assert session.post.call_count == 1  # no retry
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_critical_stops_retrying_on_403(self):
        """403 Forbidden is a permanent error; no retry should happen."""
        sender = _make_sender()
        resp_403 = _make_response(403)
        session = _make_session(resp_403)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("HALT", critical=True)

        assert result is False
        assert session.post.call_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_critical_exponential_backoff_delays(self):
        """Backoff delays follow 1.0 * 2**attempt pattern across retries."""
        sender = _make_sender()
        resp_502a = _make_response(502)
        resp_502b = _make_response(502)
        resp_200 = _make_response(200)
        session = _make_session(resp_502a, resp_502b, resp_200)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("HALT", critical=True)

        assert result is True
        # attempt 0 fails → sleep(1.0), attempt 1 fails → sleep(2.0), attempt 2 succeeds
        mock_sleep.assert_has_awaits([call(1.0), call(2.0)])


class TestNonCriticalMessageNoRetry:
    """Non-critical messages must be fire-and-forget — no retries."""

    @pytest.mark.asyncio
    async def test_non_critical_does_not_retry_on_500(self):
        """Non-critical send on 500 returns False with a single POST attempt."""
        sender = _make_sender()
        resp_500 = _make_response(500)
        session = _make_session(resp_500)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("routine_alert", critical=False)

        assert result is False
        assert session.post.call_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_critical_does_not_retry_on_exception(self):
        """Network exception on non-critical send → returns False, no sleep."""
        sender = _make_sender()
        session = MagicMock()
        session.closed = False
        session.post = MagicMock(side_effect=OSError("timeout"))

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("info_message", critical=False)

        assert result is False
        assert session.post.call_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_critical_success_returns_true(self):
        """Sanity: non-critical send still succeeds when the API returns 200."""
        sender = _make_sender()
        resp_200 = _make_response(200)
        session = _make_session(resp_200)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("heartbeat", critical=False)

        assert result is True
        assert session.post.call_count == 1
        mock_sleep.assert_not_awaited()


class TestLastSendTimestamp:
    """_last_send_ts must only be updated on actual success."""

    @pytest.mark.asyncio
    async def test_last_send_ts_not_updated_on_failure(self):
        """A failed (non-200) send must not advance _last_send_ts."""
        sender = _make_sender()
        initial_ts = sender._last_send_ts
        resp_500 = _make_response(500)
        session = _make_session(resp_500)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            await sender.send("HALT", critical=True)

        assert sender._last_send_ts == initial_ts

    @pytest.mark.asyncio
    async def test_last_send_ts_updated_after_successful_retry(self):
        """_last_send_ts must be set when success comes on a retry attempt."""
        sender = _make_sender()
        resp_500 = _make_response(500)
        resp_200 = _make_response(200)
        session = _make_session(resp_500, resp_200)

        with (
            patch("hft_platform.notifications.telegram.aiohttp") as mock_aiohttp,
            patch("hft_platform.notifications.telegram.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_aiohttp.ClientSession.return_value = session
            mock_aiohttp.ClientTimeout = MagicMock()
            sender._session = session
            result = await sender.send("HALT", critical=True)

        assert result is True
        assert sender._last_send_ts > 0.0
