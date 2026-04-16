"""Coverage tests for notifications/telegram.py — uncovered sender/poller paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.notifications.telegram import TelegramCommandPoller, TelegramSender

# ---------------------------------------------------------------------------
# TelegramSender.__init__ — aiohttp missing path (lines 69-73)
# ---------------------------------------------------------------------------


class TestTelegramSenderInit:
    def test_enabled_without_aiohttp_disables(self, monkeypatch):
        """When aiohttp is None and enabled=True, sender disables itself."""
        import hft_platform.notifications.telegram as tel_mod

        original_aiohttp = tel_mod.aiohttp
        try:
            tel_mod.aiohttp = None
            sender = TelegramSender(
                bot_token="fake_token",
                chat_id="12345",
                enabled=True,
            )
            assert sender._enabled is False
        finally:
            tel_mod.aiohttp = original_aiohttp

    def test_disabled_by_default(self):
        sender = TelegramSender()
        assert sender._enabled is False

    def test_enabled_with_credentials(self):
        sender = TelegramSender(
            bot_token="fake_token",
            chat_id="12345",
            enabled=True,
        )
        assert sender._enabled is True


# ---------------------------------------------------------------------------
# TelegramSender.send — oversized message splitting (lines 92-98)
# ---------------------------------------------------------------------------


class TestSendOversizedMessage:
    @pytest.mark.asyncio
    async def test_send_oversized_splits_into_chunks(self):
        """Messages over 4096 chars should be split and sent."""
        sender = TelegramSender(
            bot_token="fake_token",
            chat_id="12345",
            enabled=True,
        )

        # Mock the aiohttp session to return 200 for all posts
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_resp)
        sender._session = mock_session

        long_text = "A" * 5000
        result = await sender.send(long_text, critical=True)
        assert result is True
        # At least 2 POST calls (5000 / 4096 = 2 chunks)
        assert mock_session.post.call_count >= 2

    @pytest.mark.asyncio
    async def test_send_oversized_partial_failure(self):
        """If one chunk's HTTP call fails, overall result is False."""
        sender = TelegramSender(
            bot_token="fake_token",
            chat_id="12345",
            enabled=True,
        )

        call_count = 0

        def make_resp():
            nonlocal call_count
            call_count += 1
            mock_resp = AsyncMock()
            if call_count == 1:
                mock_resp.status = 500
                mock_resp.text = AsyncMock(return_value="server error")
            else:
                mock_resp.status = 200
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            return mock_resp

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=lambda *a, **k: make_resp())
        sender._session = mock_session

        long_text = "A" * 5000
        result = await sender.send(long_text)
        assert result is False


# ---------------------------------------------------------------------------
# TelegramSender._split_text — edge cases (lines 105-117)
# ---------------------------------------------------------------------------


class TestSplitText:
    def test_split_on_newline(self):
        text = "line1\nline2\nline3\nline4\nline5"
        chunks = TelegramSender._split_text(text, 12)
        for chunk in chunks:
            assert len(chunk) <= 12

    def test_split_no_newlines_hard_split(self):
        """No newlines forces hard split at max_len."""
        text = "A" * 100
        chunks = TelegramSender._split_text(text, 30)
        assert len(chunks) >= 4
        for chunk in chunks:
            assert len(chunk) <= 30

    def test_split_short_text_single_chunk(self):
        text = "short"
        chunks = TelegramSender._split_text(text, 100)
        assert chunks == ["short"]


# ---------------------------------------------------------------------------
# TelegramSender.close — session handling (lines 198, 202-206)
# ---------------------------------------------------------------------------


class TestTelegramSenderClose:
    @pytest.mark.asyncio
    async def test_close_with_no_session(self):
        sender = TelegramSender()
        await sender.close()
        assert sender._session is None

    @pytest.mark.asyncio
    async def test_close_with_open_session(self):
        sender = TelegramSender()
        mock_session = AsyncMock()
        mock_session.closed = False
        sender._session = mock_session
        await sender.close()
        mock_session.close.assert_awaited_once()
        assert sender._session is None

    @pytest.mark.asyncio
    async def test_close_with_already_closed_session(self):
        sender = TelegramSender()
        mock_session = AsyncMock()
        mock_session.closed = True
        sender._session = mock_session
        await sender.close()
        mock_session.close.assert_not_awaited()
        assert sender._session is None


# ---------------------------------------------------------------------------
# TelegramSender._send_single — exception paths (lines 178-198)
# ---------------------------------------------------------------------------


class TestSendSingleExceptionPaths:
    @pytest.mark.asyncio
    async def test_non_critical_exception_returns_false(self):
        """Non-critical send that raises returns False immediately."""
        sender = TelegramSender(
            bot_token="fake_token",
            chat_id="12345",
            enabled=True,
        )
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=RuntimeError("connection refused"))
        mock_session.close = AsyncMock()
        sender._session = mock_session

        result = await sender._send_single("test", critical=False)
        assert result is False

    @pytest.mark.asyncio
    async def test_send_disabled_returns_false(self):
        sender = TelegramSender()
        result = await sender.send("test")
        assert result is False


# ---------------------------------------------------------------------------
# TelegramCommandPoller._reply — exception path (lines 245-246)
# ---------------------------------------------------------------------------


class TestPollerReply:
    @pytest.mark.asyncio
    async def test_reply_exception_handled(self):
        """_reply swallows exceptions from post."""
        poller = TelegramCommandPoller(
            bot_token="fake_token",
            chat_id="12345",
            redis_client=MagicMock(),
        )
        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=RuntimeError("connection error"))
        await poller._reply(mock_session, "test reply")


# ---------------------------------------------------------------------------
# TelegramCommandPoller.poll_once — various paths (lines 262-291)
# ---------------------------------------------------------------------------


class TestPollerPollOnce:
    @pytest.mark.asyncio
    async def test_poll_bad_status(self):
        """poll_once with non-200 status returns early."""
        poller = TelegramCommandPoller(
            bot_token="fake_token",
            chat_id="12345",
            redis_client=MagicMock(),
        )
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        poller._session = mock_session

        await poller.poll_once()

    @pytest.mark.asyncio
    async def test_poll_exception_handled(self):
        """poll_once catches and logs network exceptions."""
        poller = TelegramCommandPoller(
            bot_token="fake_token",
            chat_id="12345",
            redis_client=MagicMock(),
        )
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(side_effect=RuntimeError("network error"))
        poller._session = mock_session

        # Should not raise
        await poller.poll_once()


# ---------------------------------------------------------------------------
# TelegramCommandPoller.close (lines 303-311)
# ---------------------------------------------------------------------------


class TestPollerClose:
    @pytest.mark.asyncio
    async def test_close_no_session(self):
        poller = TelegramCommandPoller(
            bot_token="t", chat_id="c", redis_client=MagicMock()
        )
        await poller.close()
        assert poller._session is None

    @pytest.mark.asyncio
    async def test_close_open_session(self):
        poller = TelegramCommandPoller(
            bot_token="t", chat_id="c", redis_client=MagicMock()
        )
        mock_session = AsyncMock()
        mock_session.closed = False
        poller._session = mock_session
        await poller.close()
        mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_already_closed_session(self):
        poller = TelegramCommandPoller(
            bot_token="t", chat_id="c", redis_client=MagicMock()
        )
        mock_session = AsyncMock()
        mock_session.closed = True
        poller._session = mock_session
        await poller.close()
        mock_session.close.assert_not_awaited()
