"""Unit tests for bot scheduled push jobs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.reports.models import ComposedReport, MessagePart


def _make_composed(msgs: list[str] | None = None) -> ComposedReport:
    if msgs is None:
        msgs = ["msg1", "msg2"]
    return ComposedReport(messages=[MessagePart(kind="text", content=m, min_tier="paid") for m in msgs])


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "12345")
    import hft_platform.bot.app as bot_app

    bot_app.latest_manual_report_context = None


class TestScheduleJobs:
    def test_registers_two_daily_jobs_and_heartbeat(self) -> None:
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        assert job_queue.run_daily.call_count == 2
        assert job_queue.run_repeating.call_count == 1

    # python-telegram-bot maps run_daily(days=...) 0-6 to SUNDAY-SATURDAY; it
    # mapped them to Monday-Sunday before v20.0. These tests name the weekdays
    # rather than asserting raw ints, because the previous versions asserted
    # {0,1,2,3,4} and {0,1,2,3,4,5} -- which is what the code shipped, and which
    # under PTB 22.8 means Sun-Thu and Sun-Fri. The tests were pinning the bug.
    _PTB_WEEKDAY = {
        "sunday": 0,
        "monday": 1,
        "tuesday": 2,
        "wednesday": 3,
        "thursday": 4,
        "friday": 5,
        "saturday": 6,
    }

    def test_the_day_report_runs_every_weekday_and_only_weekdays(self) -> None:
        """13:50 covers the day session that closed at 13:45, Monday to Friday.

        Friday was the casualty of the old numbering: 4 meant Thursday, so the
        Friday day-session report was never sent. THESHOW's heartbeat still read
        ``last_day=2026-08-20`` (a Thursday) on Saturday 2026-08-22.
        """
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        call_kwargs = job_queue.run_daily.call_args_list[0]
        assert call_kwargs.kwargs["time"].hour == 13
        assert call_kwargs.kwargs["time"].minute == 50
        expected = {self._PTB_WEEKDAY[d] for d in ("monday", "tuesday", "wednesday", "thursday", "friday")}
        assert set(call_kwargs.kwargs["days"]) == expected

    def test_the_night_report_runs_tuesday_through_saturday(self) -> None:
        """05:05 covers the night session that just closed at 05:00.

        A TAIFEX night session opens 15:00 on a trading day and closes 05:00 the
        next calendar day, so sessions run Mon->Tue through Fri->Sat and the
        reports land Tuesday through Saturday.

        Saturday is the one that matters and the one the old numbering dropped
        (5 meant Friday): the Saturday 05:05 run is the *Friday night session*
        report, and it never fired. Monday must be absent -- there is no
        Sunday-night session for it to report, and running it produced one of the
        two ``bot.dead_data_alert`` pages THESHOW sent every weekend.
        """
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        call_kwargs = job_queue.run_daily.call_args_list[1]
        assert call_kwargs.kwargs["time"].hour == 5
        assert call_kwargs.kwargs["time"].minute == 5
        days = set(call_kwargs.kwargs["days"])
        expected = {self._PTB_WEEKDAY[d] for d in ("tuesday", "wednesday", "thursday", "friday", "saturday")}
        assert days == expected
        assert self._PTB_WEEKDAY["saturday"] in days, "the Friday night session report would be lost"
        assert self._PTB_WEEKDAY["sunday"] not in days
        assert self._PTB_WEEKDAY["monday"] not in days, "there is no Sunday-night session to report"


class TestPushJob:
    @pytest.mark.asyncio
    async def test_push_sends_messages_on_success(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        with patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_build:
            mock_build.return_value = SimpleNamespace(
                composed=_make_composed(["msg1", "msg2"]),
                dossier=MagicMock(),
                decision=MagicMock(),
                llm_error=None,
            )
            with patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio:
                mock_asyncio.sleep = AsyncMock()
                await _push_report(ctx, "day")

        assert ctx.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_push_no_data_does_nothing(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_build:
            mock_build.return_value = SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)
            await _push_report(ctx, "day")

        ctx.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_no_chat_id_does_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hft_platform.bot.scheduler import _push_report

        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        await _push_report(ctx, "day")

        ctx.bot.send_message.assert_not_called()


class TestMultiSymbolPush:
    @pytest.mark.asyncio
    async def test_push_iterates_over_all_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,TMFD6,2330")
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock()) as mock_build,
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = SimpleNamespace(
                composed=_make_composed(["msg1"]),
                dossier=MagicMock(),
                decision=MagicMock(),
                llm_error=None,
            )
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        # build_hybrid_report_async should be called 3 times (one per symbol)
        assert mock_build.call_count == 3
        symbols_called = [call[0][2] for call in mock_build.call_args_list]
        assert symbols_called == ["TXFD6", "TMFD6", "2330"]

    @pytest.mark.asyncio
    async def test_push_skips_symbol_with_no_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,NOSYMBOL")
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        async def side_effect(session: str, date: object, symbol: str) -> SimpleNamespace:
            if symbol == "NOSYMBOL":
                return SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)
            return SimpleNamespace(
                composed=_make_composed(["msg1"]),
                dossier=MagicMock(),
                decision=MagicMock(),
                llm_error=None,
            )

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(side_effect=side_effect)),
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        # Only 1 message sent (TXFD6), NOSYMBOL skipped
        assert ctx.bot.send_message.call_count == 1


class TestPushTimestampTracking:
    @pytest.mark.asyncio
    async def test_push_does_not_update_timestamp_when_all_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "NOSYM1,NOSYM2")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        bot_app.last_day_report = None
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch(
            "hft_platform.reports.pipeline.build_hybrid_report_async",
            new=AsyncMock(return_value=SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)),
        ):
            await _push_report(ctx, "day")

        assert bot_app.last_day_report is None

    @pytest.mark.asyncio
    async def test_push_updates_timestamp_when_any_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,NOSYMBOL")
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        bot_app.last_day_report = None
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        async def side_effect(session: str, date: object, symbol: str) -> SimpleNamespace:
            if symbol == "NOSYMBOL":
                return SimpleNamespace(composed=None, dossier=None, decision=None, llm_error=None)
            return SimpleNamespace(
                composed=_make_composed(["msg1"]),
                dossier=MagicMock(),
                decision=MagicMock(),
                llm_error=None,
            )

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(side_effect=side_effect)),
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        assert bot_app.last_day_report is not None


class TestHybridPush:
    @pytest.mark.asyncio
    async def test_push_uses_hybrid_async_builder(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        hybrid_result = SimpleNamespace(
            composed=_make_composed(["hybrid"]),
            decision=MagicMock(),
            dossier=MagicMock(),
            llm_error=None,
        )

        with (
            patch(
                "hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)
            ) as mock_hybrid,
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        mock_hybrid.assert_awaited()

    @pytest.mark.asyncio
    async def test_push_fallback_does_not_touch_manual_ask_cache(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _push_report

        existing = MagicMock(symbol="TXFD6")
        bot_app.latest_manual_report_context = existing

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.send_photo = AsyncMock()

        hybrid_result = SimpleNamespace(
            composed=_make_composed(["rule-only"]),
            decision=None,
            dossier=None,
            llm_error="timeout",
        )

        with (
            patch("hft_platform.reports.pipeline.build_hybrid_report_async", new=AsyncMock(return_value=hybrid_result)),
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        assert bot_app.latest_manual_report_context is existing


class TestDeadDataAlertRespectsTheCalendar:
    """A market that was closed is not a dead feed.

    THESHOW sent two ``bot.dead_data_alert`` pages -- each carrying the hint
    "likely an upstream feed/CK ingestion issue" -- every single weekend for at
    least a month (2026-07-26/27, 08-02/03, 08-09/10, 08-16/17, 08-23/24). The
    streak counter read "no rows" as evidence of an ingestion fault without ever
    asking whether the market had been open.
    """

    @staticmethod
    def _reset() -> None:
        import hft_platform.bot.app as bot_app

        bot_app.consecutive_empty_attempts = 0

    def test_a_closed_market_does_not_count_toward_the_dead_data_streak(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _record_empty_attempt

        self._reset()
        with patch("hft_platform.bot.scheduler._is_trading_day", return_value=False):
            for _ in range(5):
                _record_empty_attempt("night", "2026-08-23", ["TXFR1"])

        assert bot_app.consecutive_empty_attempts == 0

    def test_an_open_market_with_no_data_still_counts(self) -> None:
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _record_empty_attempt

        self._reset()
        with patch("hft_platform.bot.scheduler._is_trading_day", return_value=True):
            _record_empty_attempt("day", "2026-08-24", ["TXFR1"])
            _record_empty_attempt("night", "2026-08-24", ["TXFR1"])

        assert bot_app.consecutive_empty_attempts == 2

    def test_a_holiday_does_not_launder_an_ongoing_outage(self) -> None:
        """The streak is preserved, not reset, across a closed session.

        A feed that died on Friday is still dead on Monday. Resetting here would
        make any weekend or holiday hide an outage that spans it.
        """
        import hft_platform.bot.app as bot_app
        from hft_platform.bot.scheduler import _record_empty_attempt

        self._reset()
        with patch("hft_platform.bot.scheduler._is_trading_day", return_value=True):
            _record_empty_attempt("day", "2026-08-21", ["TXFR1"])
        with patch("hft_platform.bot.scheduler._is_trading_day", return_value=False):
            _record_empty_attempt("night", "2026-08-23", ["TXFR1"])
        with patch("hft_platform.bot.scheduler._is_trading_day", return_value=True):
            _record_empty_attempt("day", "2026-08-24", ["TXFR1"])

        assert bot_app.consecutive_empty_attempts == 2

    def test_the_trading_day_check_fails_open(self) -> None:
        """Any doubt must resolve to "alert", never to "stay quiet".

        Suppressing a real outage is the expensive mistake; one spurious page on
        a holiday is not.
        """
        from hft_platform.bot.scheduler import _is_trading_day

        assert _is_trading_day("not-a-date") is True

        with patch(
            "hft_platform.core.market_calendar.get_calendar",
            side_effect=RuntimeError("no calendar"),
        ):
            assert _is_trading_day("2026-08-24") is True

    def test_a_weekend_reads_as_closed_and_a_weekday_as_open(self) -> None:
        """The real calendar, not a mock -- weekends are the whole point."""
        from hft_platform.bot.scheduler import _is_trading_day

        assert _is_trading_day("2026-08-23") is False  # Sunday
        assert _is_trading_day("2026-08-22") is False  # Saturday
        assert _is_trading_day("2026-08-24") is True  # Monday
