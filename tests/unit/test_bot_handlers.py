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


class TestGetReportSymbols:
    def test_default_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_REPORT_SYMBOLS", raising=False)
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6"]

    def test_parses_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,TMFD6,2330")
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6", "TMFD6", "2330"]

    def test_strips_whitespace_and_uppercases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", " txfd6 , tmfd6 ")
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6", "TMFD6"]

    def test_empty_string_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "")
        from hft_platform.bot.app import get_report_symbols

        assert get_report_symbols() == ["TXFD6"]


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


class TestReportArgParsing:
    """Test /report [symbol] [day|night] positional arg parsing."""

    @pytest.mark.asyncio
    async def test_no_args_uses_default_symbol_and_auto_session(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report")
        ctx = _make_context()
        ctx.args = []
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
            patch("hft_platform.bot.app.get_report_symbols", return_value=["TXFD6", "TMFD6"]),
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            # First positional arg to build_report is session, second is date, third is symbol
            assert mock_build.call_args[0][2] == "TXFD6"

    @pytest.mark.asyncio
    async def test_symbol_only_arg(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report 2330")
        ctx = _make_context()
        ctx.args = ["2330"]
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            assert mock_build.call_args[0][2] == "2330"

    @pytest.mark.asyncio
    async def test_session_only_arg(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report night")
        ctx = _make_context()
        ctx.args = ["night"]
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            assert mock_build.call_args[0][0] == "night"

    @pytest.mark.asyncio
    async def test_symbol_and_session_args(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report TMFD6 night")
        ctx = _make_context()
        ctx.args = ["TMFD6", "night"]
        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            assert mock_build.call_args[0][0] == "night"
            assert mock_build.call_args[0][2] == "TMFD6"


class TestReportIntegration:
    """Integration test: /report with real pipeline stages but mocked CH."""

    @pytest.mark.asyncio
    async def test_full_report_flow(self) -> None:
        from hft_platform.bot.handlers import cmd_report
        from hft_platform.reports.models import (
            Bar5m,
            FlowBar,
            LargeTrade,
            SessionData,
        )

        SCALE = 10_000

        fake_sd = SessionData(
            session="day",
            symbol="TXFD6",
            date="2026-03-28",
            open=20000 * SCALE,
            high=20500 * SCALE,
            low=19500 * SCALE,
            close=20200 * SCALE,
            volume=50000,
            tick_count=1000,
            bars_5m=[
                Bar5m(
                    ts="2026-03-28 09:00:00",
                    open=20000 * SCALE,
                    high=20200 * SCALE,
                    low=19900 * SCALE,
                    close=20100 * SCALE,
                    volume=5000,
                    ticks=100,
                ),
                Bar5m(
                    ts="2026-03-28 09:05:00",
                    open=20100 * SCALE,
                    high=20500 * SCALE,
                    low=20000 * SCALE,
                    close=20300 * SCALE,
                    volume=6000,
                    ticks=120,
                ),
                Bar5m(
                    ts="2026-03-28 09:10:00",
                    open=20300 * SCALE,
                    high=20400 * SCALE,
                    low=20100 * SCALE,
                    close=20200 * SCALE,
                    volume=4000,
                    ticks=80,
                ),
            ],
            flow_5m=[
                FlowBar(
                    ts="2026-03-28 09:00:00",
                    ticks=100,
                    total_vol=5000,
                    uptick_vol=3000,
                    downtick_vol=2000,
                    flat_vol=0,
                    ud_ratio=1.5,
                    net_flow=1000,
                ),
                FlowBar(
                    ts="2026-03-28 09:05:00",
                    ticks=120,
                    total_vol=6000,
                    uptick_vol=2000,
                    downtick_vol=4000,
                    flat_vol=0,
                    ud_ratio=0.5,
                    net_flow=-2000,
                ),
                FlowBar(
                    ts="2026-03-28 09:10:00",
                    ticks=80,
                    total_vol=4000,
                    uptick_vol=1500,
                    downtick_vol=2500,
                    flat_vol=0,
                    ud_ratio=0.6,
                    net_flow=-1000,
                ),
            ],
            large_trades=[
                LargeTrade(ts="2026-03-28 09:02:00", price=20100 * SCALE, volume=30, direction="buy"),
            ],
            spread_dist={1: 500, 2: 300},
            depth_imbalance=[],
        )

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]

        with patch("hft_platform.reports.collector.DataCollector.collect") as mock_collect:
            mock_collect.return_value = fake_sd
            with patch("hft_platform.reports.collector.DataCollector.__init__", return_value=None):
                with patch("hft_platform.bot.handlers.asyncio") as mock_asyncio:
                    mock_asyncio.sleep = AsyncMock()
                    await cmd_report(update, ctx)

        # Should have sent placeholder + multiple paid messages
        assert update.message.reply_text.call_count >= 1
        send_calls = ctx.bot.send_message.call_args_list
        assert len(send_calls) >= 3  # At least summary + flow + levels
        # Verify messages are strings with content (disclaimer may be short, so threshold = 20)
        for call in send_calls:
            msg_text = call.kwargs["text"]
            assert isinstance(msg_text, str)
            assert len(msg_text) > 20
