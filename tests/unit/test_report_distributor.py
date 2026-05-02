"""Unit tests for hft_platform.reports.distributor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.reports.distributor import Distributor, ReportSender, load_channels
from hft_platform.reports.models import ChannelConfig, ComposedReport, MessagePart

# ---------------------------------------------------------------------------
# TestLoadChannels
# ---------------------------------------------------------------------------


class TestLoadChannels:
    def test_owner_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "123")
        monkeypatch.delenv("HFT_REPORT_PAID_CHANNEL_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_FREE_CHANNEL_ID", raising=False)

        channels = load_channels()

        assert len(channels) == 1
        assert channels[0].chat_id == "123"
        assert channels[0].tier == "paid"
        assert channels[0].enabled is True

    def test_all_three_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "owner_id")
        monkeypatch.setenv("HFT_REPORT_PAID_CHANNEL_ID", "paid_id")
        monkeypatch.setenv("HFT_REPORT_PAID_ENABLED", "1")
        monkeypatch.setenv("HFT_REPORT_FREE_CHANNEL_ID", "free_id")
        monkeypatch.setenv("HFT_REPORT_FREE_ENABLED", "1")

        channels = load_channels()

        assert len(channels) == 3
        tiers = {c.chat_id: c.tier for c in channels}
        assert tiers["owner_id"] == "paid"
        assert tiers["paid_id"] == "paid"
        assert tiers["free_id"] == "free"
        assert all(c.enabled for c in channels)

    def test_disabled_channel_when_enabled_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("HFT_REPORT_PAID_CHANNEL_ID", "paid_id")
        monkeypatch.delenv("HFT_REPORT_PAID_ENABLED", raising=False)
        monkeypatch.delenv("HFT_REPORT_FREE_CHANNEL_ID", raising=False)

        channels = load_channels()

        assert len(channels) == 1
        assert channels[0].chat_id == "paid_id"
        assert channels[0].enabled is False

    def test_no_env_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_PAID_CHANNEL_ID", raising=False)
        monkeypatch.delenv("HFT_REPORT_FREE_CHANNEL_ID", raising=False)

        channels = load_channels()

        assert channels == []


# ---------------------------------------------------------------------------
# TestReportSender
# ---------------------------------------------------------------------------


class TestReportSender:
    @pytest.mark.asyncio
    async def test_send_success_returns_true(self) -> None:
        sender = ReportSender(bot_token="test")

        async def _mock_post(_url: str, _payload: dict) -> tuple[int, str]:  # type: ignore[type-arg]
            return 200, '{"ok": true}'

        sender._do_post = _mock_post  # type: ignore[assignment]

        result = await sender.send("chat123", "hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_no_token_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_TELEGRAM_BOT_TOKEN", raising=False)
        sender = ReportSender(bot_token="")

        result = await sender.send("chat123", "hello")

        assert result is False


# ---------------------------------------------------------------------------
# TestDistributor
# ---------------------------------------------------------------------------


class TestDistributor:
    @pytest.mark.asyncio
    async def test_routes_by_tier(self) -> None:
        owner_channel = ChannelConfig(name="owner", chat_id="owner_id", tier="paid", enabled=True)
        free_channel = ChannelConfig(name="free", chat_id="free_id", tier="free", enabled=True)

        sender = MagicMock()
        sender.send = AsyncMock(return_value=True)

        distributor = Distributor(sender=sender, channels=[owner_channel, free_channel])

        composed = ComposedReport(
            messages=[
                MessagePart(kind="text", content="free summary", min_tier="free"),
                MessagePart(kind="text", content="paid detail", min_tier="paid"),
            ]
        )
        await distributor.send(composed)

        owner_calls = [c for c in sender.send.call_args_list if c[0][0] == "owner_id"]
        assert len(owner_calls) == 2

        free_calls = [c for c in sender.send.call_args_list if c[0][0] == "free_id"]
        assert len(free_calls) == 1
        assert free_calls[0][0][1] == "free summary"

    @pytest.mark.asyncio
    async def test_skips_disabled_channels(self) -> None:
        disabled_channel = ChannelConfig(name="disabled", chat_id="disabled_id", tier="paid", enabled=False)

        sender = MagicMock()
        sender.send = AsyncMock(return_value=True)

        distributor = Distributor(sender=sender, channels=[disabled_channel])

        composed = ComposedReport(
            messages=[
                MessagePart(kind="text", content="msg 1", min_tier="free"),
            ]
        )
        await distributor.send(composed)

        sender.send.assert_not_called()
