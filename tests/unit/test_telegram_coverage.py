"""Coverage tests for notifications/telegram.py — uncovered sender/poller paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.notifications.telegram import TelegramSender

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
# Log content: why a send failed, and never the chat id
# ---------------------------------------------------------------------------


def _session_raising(exc: BaseException) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=exc)
    session.close = AsyncMock()
    return session


class TestSendLogContent:
    @pytest.mark.asyncio
    async def test_send_exception_with_empty_str_logs_type_and_nonempty_exc(self, module_log_sink):
        """aiohttp's ClientTimeout raises TimeoutError whose str() is ''."""
        import hft_platform.notifications.telegram as tel_mod

        assert str(TimeoutError()) == ""  # the production shape (exc="" in prod logs)
        events = module_log_sink(tel_mod)
        sender = TelegramSender(bot_token="TEST", chat_id="0", enabled=True)
        sender._session = _session_raising(TimeoutError())

        result = await sender._send_single("test", critical=False)

        assert result is False
        failed = [e for e in events if e["event"] == "telegram.send_exception"]
        assert len(failed) == 1
        assert failed[0]["exc_type"] == "TimeoutError"
        assert failed[0]["exc"] != ""

    @pytest.mark.asyncio
    async def test_retry_on_exception_with_empty_str_logs_type_and_nonempty_exc(self, module_log_sink, monkeypatch):
        import hft_platform.notifications.telegram as tel_mod

        events = module_log_sink(tel_mod)
        monkeypatch.setattr(TelegramSender, "_RETRY_BACKOFF_S", 0.0)
        sender = TelegramSender(bot_token="TEST", chat_id="0", enabled=True)
        session = _session_raising(TimeoutError())
        sender._session = session

        # A failed attempt drops the session and the retry builds a new one:
        # hand it back the same mock so no real ClientSession (network) is made.
        fake_aiohttp = MagicMock()
        fake_aiohttp.ClientSession.return_value = session
        monkeypatch.setattr(tel_mod, "aiohttp", fake_aiohttp)

        result = await sender._send_single("test", critical=True)

        assert result is False
        assert session.post.call_count == TelegramSender._MAX_CRITICAL_RETRIES + 1
        retried = [e for e in events if e["event"] == "telegram.retry_on_exception"]
        assert len(retried) == TelegramSender._MAX_CRITICAL_RETRIES
        for entry in retried:
            assert entry["exc_type"] == "TimeoutError"
            assert entry["exc"] != ""
        final = [e for e in events if e["event"] == "telegram.send_exception"]
        assert len(final) == 1
        assert final[0]["exc_type"] == "TimeoutError"
        assert final[0]["exc"] != ""

    @pytest.mark.asyncio
    async def test_send_exception_with_message_keeps_the_message(self, module_log_sink):
        import hft_platform.notifications.telegram as tel_mod

        events = module_log_sink(tel_mod)
        sender = TelegramSender(bot_token="TEST", chat_id="0", enabled=True)
        sender._session = _session_raising(RuntimeError("connection refused"))

        await sender._send_single("test", critical=False)

        failed = [e for e in events if e["event"] == "telegram.send_exception"]
        assert failed[0]["exc_type"] == "RuntimeError"
        assert failed[0]["exc"] == "connection refused"

    @pytest.mark.asyncio
    async def test_sent_log_event_has_no_chat_id(self, module_log_sink):
        import hft_platform.notifications.telegram as tel_mod

        events = module_log_sink(tel_mod)
        response = AsyncMock()
        response.status = 200
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.closed = False
        session.post = MagicMock(return_value=response)
        sender = TelegramSender(bot_token="TEST", chat_id="0", enabled=True)
        sender._session = session

        result = await sender._send_single("test", critical=False)

        assert result is True
        sent = [e for e in events if e["event"] == "telegram.sent"]
        assert len(sent) == 1
        assert "chat_id" not in sent[0]
        # No log event of this send carries the chat id value under any key.
        for entry in events:
            assert "0" not in [v for v in entry.values() if isinstance(v, str)]
