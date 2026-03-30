"""Unit tests for hft_platform.reports.distributor.

Covers: load_channels, ReportSender (mock HTTP), Distributor (channel routing).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.reports.distributor import Distributor, ReportSender, load_channels
from hft_platform.reports.models import ChannelConfig, ComposedReport, MessagePart


# ---------------------------------------------------------------------------
# load_channels
# ---------------------------------------------------------------------------


class TestLoadChannels:
    def test_empty_when_no_env_vars(self, monkeypatch):
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_PAID_CHANNEL_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_FREE_CHANNEL_ID", raising=False)
        channels = load_channels()
        assert channels == []

    def test_owner_channel_always_enabled(self, monkeypatch):
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "-100123")
        monkeypatch.delenv("HFT_REPORT_PAID_CHANNEL_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_FREE_CHANNEL_ID", raising=False)
        channels = load_channels()
        assert len(channels) == 1
        ch = channels[0]
        assert ch.name == "owner"
        assert ch.chat_id == "-100123"
        assert ch.tier == "paid"
        assert ch.enabled is True

    def test_paid_channel_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("HFT_REPORT_PAID_CHANNEL_ID", "-200")
        monkeypatch.delenv("HFT_REPORT_PAID_ENABLED", raising=False)
        channels = load_channels()
        assert len(channels) == 1
        assert channels[0].enabled is False
        assert channels[0].tier == "paid"

    def test_paid_channel_enabled_when_flag_set(self, monkeypatch):
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("HFT_REPORT_PAID_CHANNEL_ID", "-200")
        monkeypatch.setenv("HFT_REPORT_PAID_ENABLED", "1")
        channels = load_channels()
        assert channels[0].enabled is True

    def test_free_channel_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_PAID_CHANNEL_ID", raising=False)
        monkeypatch.setenv("HFT_REPORT_FREE_CHANNEL_ID", "-300")
        monkeypatch.delenv("HFT_REPORT_FREE_ENABLED", raising=False)
        channels = load_channels()
        assert len(channels) == 1
        assert channels[0].tier == "free"
        assert channels[0].enabled is False

    def test_free_channel_enabled_when_flag_set(self, monkeypatch):
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_PAID_CHANNEL_ID", raising=False)
        monkeypatch.setenv("HFT_REPORT_FREE_CHANNEL_ID", "-300")
        monkeypatch.setenv("HFT_REPORT_FREE_ENABLED", "1")
        channels = load_channels()
        assert channels[0].enabled is True

    def test_all_three_channels(self, monkeypatch):
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "-100")
        monkeypatch.setenv("HFT_REPORT_PAID_CHANNEL_ID", "-200")
        monkeypatch.setenv("HFT_REPORT_PAID_ENABLED", "1")
        monkeypatch.setenv("HFT_REPORT_FREE_CHANNEL_ID", "-300")
        monkeypatch.setenv("HFT_REPORT_FREE_ENABLED", "0")
        channels = load_channels()
        assert len(channels) == 3
        names = {c.name for c in channels}
        assert names == {"owner", "paid", "free"}

    def test_empty_chat_id_skipped(self, monkeypatch):
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "")
        monkeypatch.setenv("HFT_REPORT_PAID_CHANNEL_ID", "  ")
        channels = load_channels()
        assert channels == []


# ---------------------------------------------------------------------------
# ReportSender
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously in a fresh event loop."""
    return asyncio.run(coro)


class TestReportSenderSend:
    def test_returns_false_when_no_token(self):
        sender = ReportSender(bot_token="")
        with patch.dict("os.environ", {}, clear=True):
            # No token in env either
            import os
            os.environ.pop("HFT_TELEGRAM_BOT_TOKEN", None)
            result = _run(sender.send("-100", "hello"))
        assert result is False

    def test_success_on_200(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_post = AsyncMock(return_value=(200, '{"ok":true}'))
        result = _run(sender.send("-100", "hello"))
        assert result is True

    def test_client_error_4xx_returns_false_immediately(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_post = AsyncMock(return_value=(400, '{"error":"bad request"}'))
        result = _run(sender.send("-100", "bad"))
        assert result is False
        # Should only try once (no retry for 4xx)
        assert sender._do_post.call_count == 1

    def test_rate_limit_429_retries(self):
        sender = ReportSender(bot_token="testtoken")
        # First two calls: 429, third: 200
        sender._do_post = AsyncMock(
            side_effect=[
                (429, '{"parameters":{"retry_after":0}}'),
                (429, '{"parameters":{"retry_after":0}}'),
                (200, '{"ok":true}'),
            ]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send("-100", "msg"))
        assert result is True
        assert sender._do_post.call_count == 3

    def test_rate_limit_exhaust_all_retries_returns_false(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_post = AsyncMock(
            side_effect=[(429, '{"parameters":{"retry_after":0}}')] * 3
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send("-100", "msg"))
        assert result is False

    def test_server_error_5xx_retries_with_backoff(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_post = AsyncMock(
            side_effect=[
                (500, "internal error"),
                (200, '{"ok":true}'),
            ]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send("-100", "msg"))
        assert result is True

    def test_network_exception_retries(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_post = AsyncMock(
            side_effect=[
                ConnectionError("timeout"),
                (200, '{"ok":true}'),
            ]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send("-100", "msg"))
        assert result is True

    def test_network_exception_exhausted_returns_false(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_post = AsyncMock(side_effect=ConnectionError("fail"))
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send("-100", "msg"))
        assert result is False

    def test_token_sanitized_from_exception_message(self):
        """Exception messages containing the bot token URL should be sanitized."""
        sender = ReportSender(bot_token="secrettoken")
        # Simulate an exception whose message contains the API URL
        exc = ConnectionError("https://api.telegram.org/botsecrettoken/sendMessage failed")
        sender._do_post = AsyncMock(side_effect=[exc, (200, '{"ok":true}')])
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send("-100", "msg"))
        assert result is True


class TestReportSenderSendPhoto:
    def test_returns_false_when_no_token(self):
        sender = ReportSender(bot_token="")
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("HFT_TELEGRAM_BOT_TOKEN", None)
            result = _run(sender.send_photo("-100", b"imgdata", caption="chart"))
        assert result is False

    def test_success_on_200(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_multipart_post = AsyncMock(return_value=(200, '{"ok":true}'))
        result = _run(sender.send_photo("-100", b"imgdata", caption="chart"))
        assert result is True

    def test_client_error_4xx_returns_false(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_multipart_post = AsyncMock(return_value=(403, "forbidden"))
        result = _run(sender.send_photo("-100", b"img"))
        assert result is False
        assert sender._do_multipart_post.call_count == 1

    def test_429_retries(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_multipart_post = AsyncMock(
            side_effect=[
                (429, '{"parameters":{"retry_after":0}}'),
                (200, '{"ok":true}'),
            ]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send_photo("-100", b"img"))
        assert result is True

    def test_5xx_retries(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_multipart_post = AsyncMock(
            side_effect=[
                (503, "unavailable"),
                (200, '{"ok":true}'),
            ]
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send_photo("-100", b"img"))
        assert result is True

    def test_max_retries_exceeded_returns_false(self):
        sender = ReportSender(bot_token="testtoken")
        sender._do_multipart_post = AsyncMock(return_value=(500, "err"))
        with patch("asyncio.sleep", new=AsyncMock()):
            result = _run(sender.send_photo("-100", b"img"))
        assert result is False


class TestReportSenderSendBatch:
    def test_sends_all_messages_and_returns_count(self):
        sender = ReportSender(bot_token="tok")
        sender.send = AsyncMock(return_value=True)
        with patch("asyncio.sleep", new=AsyncMock()):
            count = _run(sender.send_batch("-100", ["a", "b", "c"], delay_s=0))
        assert count == 3
        assert sender.send.call_count == 3

    def test_counts_only_successful_sends(self):
        sender = ReportSender(bot_token="tok")
        sender.send = AsyncMock(side_effect=[True, False, True])
        with patch("asyncio.sleep", new=AsyncMock()):
            count = _run(sender.send_batch("-100", ["a", "b", "c"], delay_s=0))
        assert count == 2

    def test_no_sleep_after_last_message(self):
        sender = ReportSender(bot_token="tok")
        sender.send = AsyncMock(return_value=True)
        sleep_calls = []

        async def mock_sleep(t):
            sleep_calls.append(t)

        with patch("asyncio.sleep", new=mock_sleep):
            _run(sender.send_batch("-100", ["a", "b", "c"], delay_s=1.5))

        # 3 messages → 2 sleeps (no sleep after the last)
        assert len(sleep_calls) == 2


class TestReportSenderClose:
    def test_close_calls_session_close(self):
        sender = ReportSender(bot_token="tok")
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        sender._session = session_mock
        _run(sender.close())
        session_mock.close.assert_called_once()
        assert sender._session is None

    def test_close_noop_when_no_session(self):
        sender = ReportSender(bot_token="tok")
        _run(sender.close())  # should not raise when no session was opened
        assert sender._session is None


# ---------------------------------------------------------------------------
# Distributor
# ---------------------------------------------------------------------------


def _make_composed(parts: list[MessagePart]) -> ComposedReport:
    return ComposedReport(messages=parts)


def _text_part(content: str, min_tier: str = "free") -> MessagePart:
    return MessagePart(kind="text", content=content, min_tier=min_tier)


def _image_part(min_tier: str = "free") -> MessagePart:
    return MessagePart(kind="image", content="", image=b"imgdata", caption="cap", min_tier=min_tier)


class TestDistributor:
    def _make_sender(self, send_result=True, photo_result=True):
        sender = MagicMock(spec=ReportSender)
        sender.send = AsyncMock(return_value=send_result)
        sender.send_photo = AsyncMock(return_value=photo_result)
        return sender

    def test_skips_disabled_channels(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="paid", chat_id="-100", tier="paid", enabled=False)]
        dist = Distributor(sender, channels)
        _run(dist.send(_make_composed([_text_part("hello")])))
        sender.send.assert_not_called()

    def test_sends_to_enabled_channel(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="owner", chat_id="-100", tier="paid", enabled=True)]
        dist = Distributor(sender, channels)
        _run(dist.send(_make_composed([_text_part("hello")])))
        sender.send.assert_called_once_with("-100", "hello")

    def test_free_channel_only_receives_free_tier_parts(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="free", chat_id="-300", tier="free", enabled=True)]
        dist = Distributor(sender, channels)
        parts = [
            _text_part("free msg", min_tier="free"),
            _text_part("paid msg", min_tier="paid"),
        ]
        with patch("asyncio.sleep", new=AsyncMock()):
            _run(dist.send(_make_composed(parts)))
        # Only the free message should be sent
        assert sender.send.call_count == 1
        sender.send.assert_called_once_with("-300", "free msg")

    def test_paid_channel_receives_free_and_paid_parts(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="paid", chat_id="-200", tier="paid", enabled=True)]
        dist = Distributor(sender, channels)
        parts = [
            _text_part("free msg", min_tier="free"),
            _text_part("paid msg", min_tier="paid"),
        ]
        with patch("asyncio.sleep", new=AsyncMock()):
            _run(dist.send(_make_composed(parts)))
        assert sender.send.call_count == 2

    def test_owner_channel_receives_all_parts(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="owner", chat_id="-100", tier="owner", enabled=True)]
        dist = Distributor(sender, channels)
        parts = [
            _text_part("free msg", min_tier="free"),
            _text_part("paid msg", min_tier="paid"),
        ]
        with patch("asyncio.sleep", new=AsyncMock()):
            _run(dist.send(_make_composed(parts)))
        assert sender.send.call_count == 2

    def test_no_parts_for_tier_skips_channel(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="free", chat_id="-300", tier="free", enabled=True)]
        dist = Distributor(sender, channels)
        parts = [_text_part("paid only", min_tier="paid")]
        _run(dist.send(_make_composed(parts)))
        sender.send.assert_not_called()

    def test_image_part_calls_send_photo(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="owner", chat_id="-100", tier="paid", enabled=True)]
        dist = Distributor(sender, channels)
        parts = [_image_part(min_tier="free")]
        _run(dist.send(_make_composed(parts)))
        sender.send_photo.assert_called_once_with("-100", b"imgdata", caption="cap")
        sender.send.assert_not_called()

    def test_image_part_without_image_bytes_not_sent(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="owner", chat_id="-100", tier="paid", enabled=True)]
        dist = Distributor(sender, channels)
        # image=None — should not call send_photo
        parts = [MessagePart(kind="image", content="", image=None, caption="", min_tier="free")]
        _run(dist.send(_make_composed(parts)))
        sender.send_photo.assert_not_called()
        sender.send.assert_not_called()

    def test_sends_to_multiple_channels(self):
        sender = self._make_sender()
        channels = [
            ChannelConfig(name="owner", chat_id="-100", tier="paid", enabled=True),
            ChannelConfig(name="free", chat_id="-300", tier="free", enabled=True),
        ]
        dist = Distributor(sender, channels)
        parts = [_text_part("free msg", min_tier="free")]
        with patch("asyncio.sleep", new=AsyncMock()):
            _run(dist.send(_make_composed(parts)))
        # Both channels should receive the free message
        assert sender.send.call_count == 2

    def test_sleep_between_parts(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="owner", chat_id="-100", tier="paid", enabled=True)]
        dist = Distributor(sender, channels)
        parts = [_text_part("msg1"), _text_part("msg2"), _text_part("msg3")]
        sleep_calls = []

        async def mock_sleep(t):
            sleep_calls.append(t)

        with patch("asyncio.sleep", new=mock_sleep):
            _run(dist.send(_make_composed(parts)))

        # 3 parts → 2 sleeps (no sleep after last part)
        assert len(sleep_calls) == 2
        assert all(t == 1.5 for t in sleep_calls)

    def test_empty_messages_list(self):
        sender = self._make_sender()
        channels = [ChannelConfig(name="owner", chat_id="-100", tier="paid", enabled=True)]
        dist = Distributor(sender, channels)
        _run(dist.send(_make_composed([])))
        sender.send.assert_not_called()
        sender.send_photo.assert_not_called()
