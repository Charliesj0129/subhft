"""Unit tests for bot command handlers and access control."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

OWNER_CHAT_ID = "12345"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", OWNER_CHAT_ID)
    # Reset cached owner ID so monkeypatch takes effect
    import hft_platform.bot.app as bot_app
    bot_app._OWNER_CHAT_ID = 0


def _make_update(chat_id: int, text: str = "/start") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.args = []
    return ctx


class TestAccessControl:
    @pytest.mark.asyncio
    async def test_owner_allowed(self) -> None:
        from hft_platform.bot.app import owner_only

        @owner_only
        async def dummy_handler(update, context):
            await update.message.reply_text("ok")

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await dummy_handler(update, ctx)
        update.message.reply_text.assert_any_call("ok")

    @pytest.mark.asyncio
    async def test_non_owner_rejected(self) -> None:
        from hft_platform.bot.app import owner_only

        called = False

        @owner_only
        async def dummy_handler(update, context):
            nonlocal called
            called = True

        update = _make_update(chat_id=99999)
        ctx = _make_context()
        await dummy_handler(update, ctx)
        assert not called
        update.message.reply_text.assert_called_once_with("未授權")


class TestStartHandler:
    @pytest.mark.asyncio
    async def test_start_replies_with_menu(self) -> None:
        from hft_platform.bot.handlers import cmd_start
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_start(update, ctx)
        reply_text = update.message.reply_text.call_args[0][0]
        assert "/report" in reply_text
        assert "/levels" in reply_text
        assert "/flow" in reply_text
        assert "/status" in reply_text


class TestReportHandler:
    @pytest.mark.asyncio
    async def test_report_sends_paid_messages(self) -> None:
        from hft_platform.bot.handlers import cmd_report
        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]
        with patch("hft_platform.reports.pipeline.build_report") as mock_build:
            mock_build.return_value = {"paid": ["msg1", "msg2"], "free": ["msg1"]}
            with patch("hft_platform.bot.handlers.asyncio") as mock_asyncio:
                mock_asyncio.sleep = AsyncMock()
                await cmd_report(update, ctx)
        calls = update.message.reply_text.call_args_list
        assert "產生報告中" in calls[0][0][0]
        send_calls = ctx.bot.send_message.call_args_list
        assert len(send_calls) == 2
        assert send_calls[0].kwargs["text"] == "msg1"
        assert send_calls[1].kwargs["text"] == "msg2"

    @pytest.mark.asyncio
    async def test_report_no_data_replies_message(self) -> None:
        from hft_platform.bot.handlers import cmd_report
        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]
        with patch("hft_platform.reports.pipeline.build_report") as mock_build:
            mock_build.return_value = None
            await cmd_report(update, ctx)
        calls = update.message.reply_text.call_args_list
        assert any("無交易資料" in str(c) for c in calls)


class TestStatusHandler:
    @pytest.mark.asyncio
    async def test_status_includes_uptime(self) -> None:
        from hft_platform.bot.handlers import cmd_status
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_status(update, ctx)
        reply_text = update.message.reply_text.call_args[0][0]
        assert "運行時間" in reply_text


class TestLevelsHandler:
    @pytest.mark.asyncio
    async def test_levels_returns_sr_text(self) -> None:
        from hft_platform.bot.handlers import cmd_levels
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TXFD6"
        fake_signal = MagicMock()
        fake_signal.supports = []
        fake_signal.resistances = []
        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect,
            patch("hft_platform.reports.signals.SignalEngine") as MockSignal,
        ):
            mock_collect.return_value = fake_sd
            MockSignal.return_value.analyze.return_value = fake_signal
            await cmd_levels(update, ctx)
        reply_text = update.message.reply_text.call_args[0][0]
        assert "支撐壓力位" in reply_text


class TestFlowHandler:
    @pytest.mark.asyncio
    async def test_flow_returns_summary(self) -> None:
        from hft_platform.bot.handlers import cmd_flow
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TXFD6"
        fake_sd.volume = 50000
        fake_sd.flow_5m = []
        fake_sd.large_trades = []
        with patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect:
            mock_collect.return_value = fake_sd
            await cmd_flow(update, ctx)
        reply_text = update.message.reply_text.call_args[0][0]
        assert "流向摘要" in reply_text
