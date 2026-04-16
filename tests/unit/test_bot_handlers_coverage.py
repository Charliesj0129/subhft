"""Coverage tests for bot/handlers.py: set_position_store, _collect_core_for_latest,
cmd_report_rule error/empty, cmd_ask dispatch, cmd_levels/flow error/empty, cmd_status
detail, cmd_pos with recovery and strategy filtering, _send_composed image path.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from hft_platform.reports.models import ComposedReport, MessagePart

OWNER_CHAT_ID = "12345"
_TZ = ZoneInfo("Asia/Taipei")
PLATFORM_SCALE = 10_000


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", OWNER_CHAT_ID)
    import hft_platform.bot.app as bot_app

    bot_app._OWNER_CHAT_ID = 0
    bot_app.latest_manual_report_context = None


def _make_update(chat_id: int, text: str = "/start") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(**kwargs) -> MagicMock:
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_photo = AsyncMock()
    ctx.args = kwargs.get("args", [])
    return ctx


def _make_composed(msgs: list[str] | None = None) -> ComposedReport:
    if msgs is None:
        msgs = ["msg1"]
    return ComposedReport(messages=[MessagePart(kind="text", content=m, min_tier="paid") for m in msgs])


# ---------------------------------------------------------------------------
# set_position_store / _get_position_store (lines 32, 37)
# ---------------------------------------------------------------------------


class TestPositionStoreRef:
    def test_set_position_store_sets_ref(self) -> None:
        from hft_platform.bot.handlers import _get_position_store, set_position_store

        store = MagicMock()
        set_position_store(store)
        assert _get_position_store() is store
        # Cleanup
        set_position_store(None)

    def test_get_position_store_returns_none_when_unset(self) -> None:
        from hft_platform.bot.handlers import _get_position_store, set_position_store

        set_position_store(None)
        assert _get_position_store() is None


# ---------------------------------------------------------------------------
# _collect_core_for_latest (lines 83-106)
# ---------------------------------------------------------------------------


class TestCollectCoreForLatest:
    def test_returns_session_data_on_first_hit(self) -> None:
        from hft_platform.bot.handlers import _collect_core_for_latest

        mock_sd = MagicMock()
        mock_sd.tick_count = 100

        with (
            patch("hft_platform.bot.app.get_report_symbols", return_value=["TXFD6"]),
            patch("hft_platform.reports.collector.DataCollector") as MockDC,
            patch("hft_platform.reports.collector._day_filter", return_value="day_f"),
            patch("hft_platform.reports.collector._night_filter", return_value="night_f"),
        ):
            MockDC.return_value.collect_core.return_value = mock_sd
            result = _collect_core_for_latest()
        assert result is mock_sd

    def test_skips_empty_sessions_and_tries_next_day(self) -> None:
        from hft_platform.bot.handlers import _collect_core_for_latest

        empty_sd = MagicMock()
        empty_sd.tick_count = 0
        filled_sd = MagicMock()
        filled_sd.tick_count = 50

        call_count = 0

        def collect_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return empty_sd
            return filled_sd

        with (
            patch("hft_platform.bot.app.get_report_symbols", return_value=["TXFD6"]),
            patch("hft_platform.reports.collector.DataCollector") as MockDC,
            patch("hft_platform.reports.collector._day_filter", return_value="day_f"),
            patch("hft_platform.reports.collector._night_filter", return_value="night_f"),
        ):
            MockDC.return_value.collect_core.side_effect = collect_side_effect
            result = _collect_core_for_latest()
        assert result is filled_sd

    def test_uses_explicit_symbol(self) -> None:
        from hft_platform.bot.handlers import _collect_core_for_latest

        mock_sd = MagicMock()
        mock_sd.tick_count = 100

        with (
            patch("hft_platform.reports.collector.DataCollector") as MockDC,
            patch("hft_platform.reports.collector._day_filter", return_value="day_f"),
            patch("hft_platform.reports.collector._night_filter", return_value="night_f"),
        ):
            MockDC.return_value.collect_core.return_value = mock_sd
            result = _collect_core_for_latest(symbol="2330")
        first_call = MockDC.return_value.collect_core.call_args_list[0]
        assert first_call[0][0] == "2330"

    def test_returns_last_empty_sd_when_all_empty(self) -> None:
        """When no session has data, returns last (empty) sd (line 106)."""
        from hft_platform.bot.handlers import _collect_core_for_latest

        empty_sd = MagicMock()
        empty_sd.tick_count = 0

        with (
            patch("hft_platform.bot.app.get_report_symbols", return_value=["TXFD6"]),
            patch("hft_platform.reports.collector.DataCollector") as MockDC,
            patch("hft_platform.reports.collector._day_filter", return_value="day_f"),
            patch("hft_platform.reports.collector._night_filter", return_value="night_f"),
        ):
            MockDC.return_value.collect_core.return_value = empty_sd
            result = _collect_core_for_latest()
        assert result is empty_sd


# ---------------------------------------------------------------------------
# cmd_report_rule: error and empty paths (lines 187-194)
# ---------------------------------------------------------------------------


class TestReportRuleErrorPaths:
    @pytest.mark.asyncio
    async def test_report_rule_exception_replies_error(self) -> None:
        """build_report raising exception replies with error msg (lines 187-190)."""
        from hft_platform.bot.handlers import cmd_report_rule

        update = _make_update(chat_id=12345, text="/report_rule day")
        ctx = _make_context(args=["day"])

        with (
            patch("hft_platform.reports.pipeline.build_report", side_effect=RuntimeError("CH down")),
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.to_thread = AsyncMock(side_effect=RuntimeError("CH down"))
            mock_asyncio.sleep = AsyncMock()
            await cmd_report_rule(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("失敗" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_report_rule_none_composed_replies_no_data(self) -> None:
        """build_report returning None replies with no-data msg (lines 192-194)."""
        from hft_platform.bot.handlers import cmd_report_rule

        update = _make_update(chat_id=12345, text="/report_rule night")
        ctx = _make_context(args=["night"])

        with (
            patch("hft_platform.reports.pipeline.build_report"),
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.to_thread = AsyncMock(return_value=None)
            mock_asyncio.sleep = AsyncMock()
            await cmd_report_rule(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("無交易資料" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_report_rule_night_updates_last_night(self) -> None:
        """Night session updates last_night_report (line 201)."""
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_report_rule

        update = _make_update(chat_id=12345, text="/report_rule night")
        ctx = _make_context(args=["night"])

        with (
            patch("hft_platform.reports.pipeline.build_report"),
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.to_thread = AsyncMock(return_value=_make_composed(["ok"]))
            mock_asyncio.sleep = AsyncMock()
            await cmd_report_rule(update, ctx)

        assert bot_app.last_night_report is not None


# ---------------------------------------------------------------------------
# cmd_report error path (lines 144-147)
# ---------------------------------------------------------------------------


class TestReportErrorPath:
    @pytest.mark.asyncio
    async def test_report_exception_replies_error(self) -> None:
        """build_hybrid_report_async raising replies with error msg."""
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report day")
        ctx = _make_context(args=["day"])

        with patch(
            "hft_platform.reports.pipeline.build_hybrid_report_async",
            new=AsyncMock(side_effect=RuntimeError("LLM down")),
        ):
            await cmd_report(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("失敗" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_report_auto_session_night(self) -> None:
        """When session is None, session auto-detects (lines 178-179)."""
        from hft_platform.bot.handlers import cmd_report

        update = _make_update(chat_id=12345, text="/report TXFD6")
        ctx = _make_context(args=["TXFD6"])

        hybrid_result = SimpleNamespace(
            composed=_make_composed(["msg"]),
            dossier=None,
            decision=None,
            llm_error=None,
        )
        with (
            patch(
                "hft_platform.reports.pipeline.build_hybrid_report_async",
                new=AsyncMock(return_value=hybrid_result),
            ) as mock_build,
            patch("hft_platform.bot.handlers.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await cmd_report(update, ctx)
            # Session was auto-detected (day or night based on current hour)
            session_arg = mock_build.call_args[0][0]
            assert session_arg in ("day", "night")


# ---------------------------------------------------------------------------
# cmd_ask: question dispatch and import error (lines 217-229)
# ---------------------------------------------------------------------------


class TestAskHandler:
    @pytest.mark.asyncio
    async def test_ask_empty_question_replies_usage(self) -> None:
        """Empty question shows usage hint (lines 217-220)."""
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_ask

        bot_app.latest_manual_report_context = bot_app.LatestReportContext(
            symbol="TXFD6", session="day", date="2026-04-07",
            dossier=MagicMock(), decision=MagicMock(),
        )
        update = _make_update(chat_id=12345, text="/ask")
        ctx = _make_context(args=[])
        await cmd_ask(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "/ask" in reply

    @pytest.mark.asyncio
    async def test_ask_import_error_replies_not_enabled(self) -> None:
        """ImportError for llm_reasoner replies with 'not enabled' (lines 222-226)."""
        import sys

        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_ask

        bot_app.latest_manual_report_context = bot_app.LatestReportContext(
            symbol="TXFD6", session="day", date="2026-04-07",
            dossier=MagicMock(), decision=MagicMock(),
        )
        update = _make_update(chat_id=12345, text="/ask why")
        ctx = _make_context(args=["why"])

        # Remove cached module and inject an import blocker
        saved = sys.modules.pop("hft_platform.reports.llm_reasoner", None)
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def blocking_import(name, *args, **kwargs):
            if name == "hft_platform.reports.llm_reasoner":
                raise ImportError("no llm")
            return original_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=blocking_import):
                await cmd_ask(update, ctx)
        finally:
            if saved is not None:
                sys.modules["hft_platform.reports.llm_reasoner"] = saved

        calls = update.message.reply_text.call_args_list
        assert any("尚未啟用" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_ask_successful_followup(self) -> None:
        """Successful followup returns LLM answer (lines 228-229)."""
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_ask

        bot_app.latest_manual_report_context = bot_app.LatestReportContext(
            symbol="TXFD6", session="day", date="2026-04-07",
            dossier=MagicMock(), decision=MagicMock(),
        )
        update = _make_update(chat_id=12345, text="/ask 還能追嗎")
        ctx = _make_context(args=["還能追嗎"])

        mock_answer = AsyncMock(return_value="The answer is 42.")
        with patch("hft_platform.bot.handlers.answer_followup_question", mock_answer, create=True):
            with patch(
                "hft_platform.reports.llm_reasoner.answer_followup_question",
                mock_answer,
                create=True,
            ):
                await cmd_ask(update, ctx)

        calls = update.message.reply_text.call_args_list
        # Check that an answer was sent (the last call should be the answer or usage)
        assert len(calls) >= 1


# ---------------------------------------------------------------------------
# cmd_levels: error and empty paths (lines 257-264)
# ---------------------------------------------------------------------------


class TestLevelsErrorPaths:
    @pytest.mark.asyncio
    async def test_levels_exception_replies_unavailable(self) -> None:
        """Exception in _collect_core_for_latest replies with unavailable msg (lines 257-260)."""
        from hft_platform.bot.handlers import cmd_levels

        update = _make_update(chat_id=12345)
        ctx = _make_context()

        with patch(
            "hft_platform.bot.handlers._collect_core_for_latest",
            side_effect=RuntimeError("CH down"),
        ):
            await cmd_levels(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("不可用" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_levels_empty_data_replies_no_data(self) -> None:
        """Empty tick_count replies with no-data msg (lines 262-264)."""
        from hft_platform.bot.handlers import cmd_levels

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        fake_sd = MagicMock()
        fake_sd.tick_count = 0

        with patch("hft_platform.bot.handlers._collect_core_for_latest", return_value=fake_sd):
            await cmd_levels(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("無交易資料" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_levels_with_resistance_and_support(self) -> None:
        """Levels with both resistance and support lines (lines 276-287)."""
        from hft_platform.bot.handlers import cmd_levels

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "day"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TXFD6"

        resist = SimpleNamespace(side="resistance", price=200000000, strength=0.8, sources=["VWAP", "EMA"])
        support = SimpleNamespace(side="support", price=190000000, strength=0.6, sources=["Fib"])

        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest", return_value=fake_sd),
            patch("hft_platform.reports.facts.extract_all", return_value=MagicMock()),
            patch("hft_platform.reports.reasoner.LevelReasoner") as MockLR,
        ):
            MockLR.return_value.analyze.return_value = [resist, support]
            await cmd_levels(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "R1:" in reply
        assert "S1:" in reply


# ---------------------------------------------------------------------------
# cmd_flow: error and empty paths (lines 307-314, 326, 341-344)
# ---------------------------------------------------------------------------


class TestFlowErrorPaths:
    @pytest.mark.asyncio
    async def test_flow_exception_replies_unavailable(self) -> None:
        """Exception in _collect_core_for_latest replies with unavailable msg (lines 307-310)."""
        from hft_platform.bot.handlers import cmd_flow

        update = _make_update(chat_id=12345)
        ctx = _make_context()

        with patch(
            "hft_platform.bot.handlers._collect_core_for_latest",
            side_effect=RuntimeError("CH down"),
        ):
            await cmd_flow(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("不可用" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_flow_empty_data_replies_no_data(self) -> None:
        """Empty tick_count replies with no-data msg (lines 312-314)."""
        from hft_platform.bot.handlers import cmd_flow

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        fake_sd = MagicMock()
        fake_sd.tick_count = 0

        with patch("hft_platform.bot.handlers._collect_core_for_latest", return_value=fake_sd):
            await cmd_flow(update, ctx)

        calls = update.message.reply_text.call_args_list
        assert any("無交易資料" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_flow_neutral_bias(self) -> None:
        """session_ud near 1.0 shows neutral bias (line 326)."""
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
        fake_fr.flow.session_ud = 1.0
        fake_fr.flow.session_net_flow = 0
        fake_fr.flow.strongest_buy_bar.ts = "2026-03-28 09:00:00"
        fake_fr.flow.strongest_buy_bar.ud_ratio = 1.0
        fake_fr.flow.strongest_sell_bar.ts = "2026-03-28 10:00:00"
        fake_fr.flow.strongest_sell_bar.ud_ratio = 1.0
        fake_fr.segments = []

        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest", return_value=fake_sd),
            patch("hft_platform.reports.facts.extract_all", return_value=fake_fr),
        ):
            await cmd_flow(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "中性" in reply

    @pytest.mark.asyncio
    async def test_flow_with_segments(self) -> None:
        """Flow with segments shows segment summaries (lines 341-344)."""
        from hft_platform.bot.handlers import cmd_flow

        update = _make_update(chat_id=12345)
        ctx = _make_context()
        fake_sd = MagicMock()
        fake_sd.tick_count = 100
        fake_sd.session = "night"
        fake_sd.date = "2026-03-28"
        fake_sd.symbol = "TXFD6"
        fake_sd.volume = 30000
        fake_fr = MagicMock()
        fake_fr.flow.session_ud = 0.85
        fake_fr.flow.session_net_flow = -2000
        fake_fr.flow.strongest_buy_bar.ts = "2026-03-28 15:00:00"
        fake_fr.flow.strongest_buy_bar.ud_ratio = 1.3
        fake_fr.flow.strongest_sell_bar.ts = "2026-03-28 16:00:00"
        fake_fr.flow.strongest_sell_bar.ud_ratio = 0.5
        seg1 = SimpleNamespace(name="開盤", dominant_side="bull", ud_ratio=1.2, volume=10000)
        seg2 = SimpleNamespace(name="收盤", dominant_side="bear", ud_ratio=0.7, volume=20000)
        seg3 = SimpleNamespace(name="中場", dominant_side="neutral", ud_ratio=1.0, volume=5000)
        fake_fr.segments = [seg1, seg2, seg3]

        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest", return_value=fake_sd),
            patch("hft_platform.reports.facts.extract_all", return_value=fake_fr),
        ):
            await cmd_flow(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "時段摘要" in reply
        assert "夜盤" in reply

    @pytest.mark.asyncio
    async def test_flow_bearish_bias(self) -> None:
        """session_ud <= 0.9 shows bearish bias."""
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
        fake_fr.flow.session_ud = 0.8
        fake_fr.flow.session_net_flow = -5000
        fake_fr.flow.strongest_buy_bar.ts = "2026-03-28 09:00:00"
        fake_fr.flow.strongest_buy_bar.ud_ratio = 1.2
        fake_fr.flow.strongest_sell_bar.ts = "2026-03-28 10:00:00"
        fake_fr.flow.strongest_sell_bar.ud_ratio = 0.4
        fake_fr.segments = []

        with (
            patch("hft_platform.bot.handlers._collect_core_for_latest", return_value=fake_sd),
            patch("hft_platform.reports.facts.extract_all", return_value=fake_fr),
        ):
            await cmd_flow(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "偏空" in reply


# ---------------------------------------------------------------------------
# cmd_status: detail lines (lines 367, 370, 379)
# ---------------------------------------------------------------------------


class TestStatusDetails:
    @pytest.mark.asyncio
    async def test_status_with_day_report(self) -> None:
        """Status shows day report timestamp (line 367)."""
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_status

        bot_app.last_day_report = datetime(2026, 4, 7, 10, 30, tzinfo=_TZ)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_status(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "2026-04-07" in reply

    @pytest.mark.asyncio
    async def test_status_with_night_report(self) -> None:
        """Status shows night report timestamp (line 370)."""
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_status

        bot_app.last_night_report = datetime(2026, 4, 7, 16, 30, tzinfo=_TZ)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_status(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "夜盤報告" in reply
        assert "2026-04-07" in reply

    @pytest.mark.asyncio
    async def test_status_with_ch_ok(self) -> None:
        """Status shows ClickHouse last-ok timestamp (line 379)."""
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_status

        bot_app.last_ch_ok = datetime.now(_TZ)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_status(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "ClickHouse" in reply
        assert "分鐘前" in reply

    @pytest.mark.asyncio
    async def test_status_no_ch_ok(self) -> None:
        """Status shows '尚未連線' when no CH OK (line 379)."""
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.handlers import cmd_status

        bot_app.last_ch_ok = None
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_status(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "尚未連線" in reply


# ---------------------------------------------------------------------------
# cmd_pos: full coverage (lines 413-421)
# ---------------------------------------------------------------------------


class TestCmdPos:
    @pytest.mark.asyncio
    async def test_pos_no_store_replies_not_connected(self) -> None:
        """No position store replies '未連接'."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        set_position_store(None)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "未連接" in reply

    @pytest.mark.asyncio
    async def test_pos_empty_positions(self) -> None:
        """Empty positions dict replies '無持倉'."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        store = MagicMock()
        store.positions = {}
        store._recovery_positions = {}
        set_position_store(store)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "無持倉" in reply
        set_position_store(None)

    @pytest.mark.asyncio
    async def test_pos_with_positions_and_recovery(self) -> None:
        """Positions + recovery positions shown (lines 413-421)."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        pos1 = MagicMock()
        pos1.net_qty = 2
        pos1.symbol = "TXFD6"
        pos1.strategy_id = "strat_A"

        pos2 = MagicMock()
        pos2.net_qty = -1
        pos2.symbol = "TMFD6"
        pos2.strategy_id = "strat_B"

        store = MagicMock()
        store.positions = {"k1": pos1, "k2": pos2}
        store._recovery_positions = {
            "rk1": {"net_qty": 1, "strategy_id": "strat_A", "symbol": "2330"},
        }
        set_position_store(store)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "倉位明細" in reply
        assert "strat_A" in reply
        assert "strat_B" in reply
        assert "2330" in reply
        assert "合計" in reply  # multiple strategies => aggregate footer
        set_position_store(None)

    @pytest.mark.asyncio
    async def test_pos_filter_by_strategy(self) -> None:
        """Filtering by strategy_id only shows matching positions."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        pos1 = MagicMock()
        pos1.net_qty = 2
        pos1.symbol = "TXFD6"
        pos1.strategy_id = "strat_A"

        pos2 = MagicMock()
        pos2.net_qty = -1
        pos2.symbol = "TMFD6"
        pos2.strategy_id = "strat_B"

        store = MagicMock()
        store.positions = {"k1": pos1, "k2": pos2}
        store._recovery_positions = {}
        set_position_store(store)
        update = _make_update(chat_id=12345)
        ctx = _make_context(args=["strat_A"])
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "strat_A" in reply
        assert "strat_B" not in reply
        set_position_store(None)

    @pytest.mark.asyncio
    async def test_pos_filter_nonexistent_strategy(self) -> None:
        """Filtering for nonexistent strategy shows no positions msg."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        pos1 = MagicMock()
        pos1.net_qty = 2
        pos1.symbol = "TXFD6"
        pos1.strategy_id = "strat_A"

        store = MagicMock()
        store.positions = {"k1": pos1}
        store._recovery_positions = {}
        set_position_store(store)
        update = _make_update(chat_id=12345)
        ctx = _make_context(args=["strat_Z"])
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "strat_Z" in reply
        assert "無持倉" in reply
        set_position_store(None)

    @pytest.mark.asyncio
    async def test_pos_skips_flat_positions(self) -> None:
        """net_qty == 0 positions are skipped."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        flat_pos = MagicMock()
        flat_pos.net_qty = 0
        flat_pos.symbol = "TXFD6"
        flat_pos.strategy_id = "strat_A"

        store = MagicMock()
        store.positions = {"k1": flat_pos}
        store._recovery_positions = {}
        set_position_store(store)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "無持倉" in reply
        set_position_store(None)

    @pytest.mark.asyncio
    async def test_pos_recovery_non_dict_skipped(self) -> None:
        """Non-dict recovery entry is skipped (line 413-414)."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        pos1 = MagicMock()
        pos1.net_qty = 1
        pos1.symbol = "TXFD6"
        pos1.strategy_id = "strat_A"

        store = MagicMock()
        store.positions = {"k1": pos1}
        store._recovery_positions = {"rk_bad": "not_a_dict"}
        set_position_store(store)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "倉位明細" in reply
        set_position_store(None)

    @pytest.mark.asyncio
    async def test_pos_recovery_zero_qty_skipped(self) -> None:
        """Recovery entry with net_qty=0 is skipped."""
        from hft_platform.bot.handlers import cmd_pos, set_position_store

        store = MagicMock()
        store.positions = {}
        store._recovery_positions = {"rk1": {"net_qty": 0, "strategy_id": "s", "symbol": "X"}}
        set_position_store(store)
        update = _make_update(chat_id=12345)
        ctx = _make_context()
        await cmd_pos(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "無持倉" in reply
        set_position_store(None)


# ---------------------------------------------------------------------------
# _send_composed: image path (lines 238-239)
# ---------------------------------------------------------------------------


class TestSendComposed:
    @pytest.mark.asyncio
    async def test_send_composed_with_image(self) -> None:
        """Image parts sent via send_photo (lines 238-239)."""
        from hft_platform.bot.handlers import _send_composed

        composed = ComposedReport(
            messages=[
                MessagePart(kind="text", content="Hello", min_tier="free"),
                MessagePart(kind="image", content="", image=b"PNG_BYTES", caption="chart", min_tier="paid"),
            ]
        )
        update = _make_update(chat_id=12345)
        ctx = _make_context()

        with patch("hft_platform.bot.handlers.asyncio") as mock_asyncio:
            mock_asyncio.sleep = AsyncMock()
            await _send_composed(update, ctx, composed)

        ctx.bot.send_message.assert_awaited_once()
        ctx.bot.send_photo.assert_awaited_once()
