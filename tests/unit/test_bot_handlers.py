"""Unit tests for bot command handlers and access control."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from hft_platform.reports.models import ComposedReport, MessagePart

OWNER_CHAT_ID = "12345"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", OWNER_CHAT_ID)
    # Reset cached owner ID so monkeypatch takes effect
    import hft_platform.bot.app as bot_app

    bot_app._OWNER_CHAT_ID = 0
    bot_app.latest_manual_report_context = None


def _make_update(chat_id: int, text: str = "/start") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_photo = AsyncMock()
    ctx.args = []
    return ctx


def _make_composed(msgs: list[str] | None = None) -> ComposedReport:
    """Build a ComposedReport from a list of text strings (all paid tier)."""
    if msgs is None:
        msgs = ["msg1", "msg2"]
    return ComposedReport(messages=[MessagePart(kind="text", content=m, min_tier="paid") for m in msgs])


class TestPrevTradingDate:
    def test_weekday_unchanged(self) -> None:
        from hft_platform.bot.handlers import _prev_trading_date

        # Wednesday 2026-03-25
        wed = datetime(2026, 3, 25, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        assert _prev_trading_date(wed) == "2026-03-25"

    def test_saturday_maps_to_friday(self) -> None:
        from hft_platform.bot.handlers import _prev_trading_date

        sat = datetime(2026, 3, 28, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        assert _prev_trading_date(sat) == "2026-03-27"

    def test_sunday_maps_to_friday(self) -> None:
        from hft_platform.bot.handlers import _prev_trading_date

        sun = datetime(2026, 3, 29, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        assert _prev_trading_date(sun) == "2026-03-27"


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
    async def test_report_sends_composed_messages(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]
        hybrid_result = SimpleNamespace(
            composed=_make_composed(["msg1", "msg2"]),
            dossier=MagicMock(symbol="TXFD6", session="day", date="2026-03-28"),
            decision=MagicMock(),
            llm_error=None,
        )
        with patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)):
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
        hybrid_result = SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)
        with patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)):
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
        fake_fr = MagicMock()
        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect,
            patch("hft_platform.reports.facts.extract_all", return_value=fake_fr),
            patch("hft_platform.reports.reasoner.LevelReasoner") as MockLR,
        ):
            mock_collect.return_value = fake_sd
            MockLR.return_value.analyze.return_value = []
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
        fake_fr = MagicMock()
        fake_fr.flow.session_ud = 1.15
        fake_fr.flow.session_net_flow = 5000
        fake_fr.flow.strongest_buy_bar.ts = "2026-03-28 09:00:00"
        fake_fr.flow.strongest_buy_bar.ud_ratio = 1.8
        fake_fr.flow.strongest_sell_bar.ts = "2026-03-28 10:30:00"
        fake_fr.flow.strongest_sell_bar.ud_ratio = 0.4
        fake_fr.segments = []
        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect,
            patch("hft_platform.reports.facts.extract_all", return_value=fake_fr),
        ):
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
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
            patch("hft_platform.bot.app.get_report_symbols", return_value=["TXFD6", "TMFD6"]),
        ):
            mock_build.return_value = SimpleNamespace(
                composed=_make_composed(["msg1"]),
                dossier=None,
                decision=None,
                llm_error=None,
            )
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            # First positional arg to build_hybrid_report_async is session, second is date, third is symbol
            assert mock_build.call_args[0][2] == "TXFD6"

    @pytest.mark.asyncio
    async def test_symbol_only_arg(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report 2330")
        ctx = _make_context()
        ctx.args = ["2330"]
        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = SimpleNamespace(
                composed=_make_composed(["msg1"]),
                dossier=None,
                decision=None,
                llm_error=None,
            )
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
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = SimpleNamespace(
                composed=_make_composed(["msg1"]),
                dossier=None,
                decision=None,
                llm_error=None,
            )
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
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = SimpleNamespace(
                composed=_make_composed(["msg1"]),
                dossier=None,
                decision=None,
                llm_error=None,
            )
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            assert mock_build.call_args[0][0] == "night"
            assert mock_build.call_args[0][2] == "TMFD6"


class TestRuleOnlyAndAsk:
    @pytest.mark.asyncio
    async def test_report_rule_uses_deterministic_path_only(self) -> None:
        from hft_platform.bot.handlers import cmd_report_rule

        update = _make_update(chat_id=12345, text="/report_rule day")
        ctx = _make_context()
        ctx.args = ["day"]

        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_hybrid,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.to_thread = AsyncMock(return_value=_make_composed(["rule-only"]))
            mock_asyncio.sleep = AsyncMock()
            await cmd_report_rule(update, ctx)

        mock_asyncio.to_thread.assert_awaited_once()
        assert mock_asyncio.to_thread.await_args.args[0] is mock_build
        mock_hybrid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_report_caches_latest_manual_hybrid_context(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]

        hybrid_result = SimpleNamespace(
            composed=_make_composed(["hybrid"]),
            dossier=SimpleNamespace(symbol="TXFD6", session="day", date="2026-04-07"),
            decision=MagicMock(),
            llm_error=None,
        )

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)),
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)

        assert bot_app.latest_manual_report_context is not None
        assert bot_app.latest_manual_report_context.symbol == "TXFD6"

    @pytest.mark.asyncio
    async def test_report_clears_stale_manual_context_when_latest_run_has_no_decision(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_report

        bot_app.latest_manual_report_context = bot_app.LatestReportContext(
            symbol="OLD",
            session="day",
            date="2026-04-06",
            dossier=MagicMock(),
            decision=MagicMock(),
        )
        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]

        hybrid_result = SimpleNamespace(
            composed=_make_composed(["fallback-only"]),
            dossier=None,
            decision=None,
            llm_error="bad llm",
        )

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)),
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)

        assert bot_app.latest_manual_report_context is None

    @pytest.mark.asyncio
    async def test_ask_rejects_when_no_manual_hybrid_context(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_ask

        bot_app.latest_manual_report_context = None
        update = _make_update(chat_id=12345, text="/ask 現在還能追嗎")
        ctx = _make_context()
        ctx.args = ["現在還能追嗎"]

        await cmd_ask(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "先執行 /report" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_ask_rejects_when_latest_context_has_no_decision(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_ask

        bot_app.latest_manual_report_context = bot_app.LatestReportContext(
            symbol="TXFD6",
            session="day",
            date="2026-04-07",
            dossier=MagicMock(),
            decision=None,
        )
        update = _make_update(chat_id=12345, text="/ask 還能追嗎")
        ctx = _make_context()
        ctx.args = ["還能追嗎"]

        await cmd_ask(update, ctx)

        assert "先重新執行 /report" in update.message.reply_text.call_args[0][0]


class TestLevelsWithSymbol:
    @pytest.mark.asyncio
    async def test_levels_with_explicit_symbol(self) -> None:
        from hft_platform.bot.handlers import cmd_levels

        update = _make_update(chat_id=12345, text="/levels 2330")
        ctx = _make_context()
        ctx.args = ["2330"]
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "2330"
        fake_fr = MagicMock()
        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect,
            patch("hft_platform.reports.facts.extract_all", return_value=fake_fr),
            patch("hft_platform.reports.reasoner.LevelReasoner") as MockLR,
        ):
            mock_collect.return_value = fake_sd
            MockLR.return_value.analyze.return_value = []
            await cmd_levels(update, ctx)
        mock_collect.assert_called_once_with("2330")


class TestFlowWithSymbol:
    @pytest.mark.asyncio
    async def test_flow_with_explicit_symbol(self) -> None:
        from hft_platform.bot.handlers import cmd_flow

        update = _make_update(chat_id=12345, text="/flow TMFD6")
        ctx = _make_context()
        ctx.args = ["TMFD6"]
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TMFD6"
        fake_sd.volume = 30000
        fake_fr = MagicMock()
        fake_fr.flow.session_ud = 0.85
        fake_fr.flow.session_net_flow = -2000
        fake_fr.flow.strongest_buy_bar.ts = "2026-03-28 09:00:00"
        fake_fr.flow.strongest_buy_bar.ud_ratio = 1.3
        fake_fr.flow.strongest_sell_bar.ts = "2026-03-28 10:00:00"
        fake_fr.flow.strongest_sell_bar.ud_ratio = 0.5
        fake_fr.segments = []
        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest") as mock_collect,
            patch("hft_platform.reports.facts.extract_all", return_value=fake_fr),
        ):
            mock_collect.return_value = fake_sd
            await cmd_flow(update, ctx)
        mock_collect.assert_called_once_with("TMFD6")


class TestReportIntegration:
    """Integration test: /report with real pipeline stages but mocked CH."""

    @pytest.mark.asyncio
    async def test_full_report_flow(self) -> None:
        from hft_platform.bot.handlers import cmd_report

        composed = ComposedReport(
            messages=[
                MessagePart(
                    kind="text", content="Summary section with enough text to pass threshold check.", min_tier="free"
                ),
                MessagePart(
                    kind="text", content="Flow analysis section with detailed breakdown data.", min_tier="paid"
                ),
                MessagePart(kind="text", content="Level analysis and scenario planning details.", min_tier="paid"),
            ]
        )

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context()
        ctx.args = ["day"]
        hybrid_result = SimpleNamespace(
            composed=composed,
            dossier=SimpleNamespace(symbol="TXFD6", session="day", date="2026-04-07"),
            decision=MagicMock(),
            llm_error=None,
        )

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)),
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)

        # Should have sent placeholder + 3 composed messages
        assert update.message.reply_text.call_count >= 1
        send_calls = ctx.bot.send_message.call_args_list
        assert len(send_calls) == 3
        for call in send_calls:
            msg_text = call.kwargs["text"]
            assert isinstance(msg_text, str)
            assert len(msg_text) > 20
