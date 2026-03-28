"""Unit tests for hft_platform.reports.distributor."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.reports.distributor import Distributor, ReportSender, load_channels
from hft_platform.reports.models import ChannelConfig

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

    def test_disabled_channel_when_enabled_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        sender = ReportSender(bot_token="test")
        sender._session = mock_session

        result = await sender.send("chat123", "hello")

        assert result is True
        mock_session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_no_token_returns_false(self) -> None:
        sender = ReportSender(bot_token="")

        result = await sender.send("chat123", "hello")

        assert result is False


# ---------------------------------------------------------------------------
# TestDistributor
# ---------------------------------------------------------------------------


class TestDistributor:
    @pytest.mark.asyncio
    async def test_routes_by_tier(self) -> None:
        owner_channel = ChannelConfig(
            name="owner", chat_id="owner_id", tier="paid", enabled=True
        )
        free_channel = ChannelConfig(
            name="free", chat_id="free_id", tier="free", enabled=True
        )

        sender = MagicMock()
        sender.send_batch = AsyncMock(return_value=1)

        distributor = Distributor(sender=sender, channels=[owner_channel, free_channel])

        rendered = {
            "paid": ["paid msg 1", "paid msg 2"],
            "free": ["free msg 1"],
        }
        await distributor.send(rendered)

        calls = {call.args[0]: call.args[1] for call in sender.send_batch.call_args_list}
        assert calls["owner_id"] == ["paid msg 1", "paid msg 2"]
        assert calls["free_id"] == ["free msg 1"]

    @pytest.mark.asyncio
    async def test_skips_disabled_channels(self) -> None:
        disabled_channel = ChannelConfig(
            name="disabled", chat_id="disabled_id", tier="paid", enabled=False
        )

        sender = MagicMock()
        sender.send_batch = AsyncMock(return_value=0)

        distributor = Distributor(sender=sender, channels=[disabled_channel])

        rendered = {"paid": ["msg 1"]}
        await distributor.send(rendered)

        sender.send_batch.assert_not_called()
