"""Unit tests for bot scheduled push jobs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "12345")


class TestScheduleJobs:
    def test_registers_two_daily_jobs_and_heartbeat(self) -> None:
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        assert job_queue.run_daily.call_count == 2
        assert job_queue.run_repeating.call_count == 1

    def test_day_report_schedule(self) -> None:
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        call_kwargs = job_queue.run_daily.call_args_list[0]
        assert call_kwargs.kwargs["time"].hour == 13
        assert call_kwargs.kwargs["time"].minute == 50
        assert set(call_kwargs.kwargs["days"]) == {0, 1, 2, 3, 4}

    def test_night_report_schedule(self) -> None:
        from hft_platform.bot.scheduler import schedule_jobs

        job_queue = MagicMock()
        schedule_jobs(job_queue)

        call_kwargs = job_queue.run_daily.call_args_list[1]
        assert call_kwargs.kwargs["time"].hour == 5
        assert call_kwargs.kwargs["time"].minute == 5
        assert set(call_kwargs.kwargs["days"]) == {0, 1, 2, 3, 4, 5}


class TestPushJob:
    @pytest.mark.asyncio
    async def test_push_sends_messages_on_success(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch("hft_platform.reports.pipeline.build_report") as mock_build:
            mock_build.return_value = {"paid": ["msg1", "msg2"], "free": ["fmsg"]}
            with patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio:
                mock_asyncio.sleep = AsyncMock()
                await _push_report(ctx, "day")

        assert ctx.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_push_no_data_does_nothing(self) -> None:
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        with patch("hft_platform.reports.pipeline.build_report") as mock_build:
            mock_build.return_value = None
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

        with (
            patch("hft_platform.reports.pipeline.build_report") as mock_build,
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_build.return_value = {"paid": ["msg1"], "free": ["fmsg"]}
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        # build_report should be called 3 times (one per symbol)
        assert mock_build.call_count == 3
        symbols_called = [call[0][2] for call in mock_build.call_args_list]
        assert symbols_called == ["TXFD6", "TMFD6", "2330"]

    @pytest.mark.asyncio
    async def test_push_skips_symbol_with_no_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_REPORT_SYMBOLS", "TXFD6,NOSYMBOL")
        from hft_platform.bot.scheduler import _push_report

        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        def side_effect(session: str, date: object, symbol: str) -> dict | None:
            if symbol == "NOSYMBOL":
                return None
            return {"paid": ["msg1"], "free": ["fmsg"]}

        with (
            patch("hft_platform.reports.pipeline.build_report", side_effect=side_effect),
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

        with patch("hft_platform.reports.pipeline.build_report", return_value=None):
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

        def side_effect(session: str, date: object, symbol: str) -> dict | None:
            if symbol == "NOSYMBOL":
                return None
            return {"paid": ["msg1"], "free": ["fmsg"]}

        with (
            patch("hft_platform.reports.pipeline.build_report", side_effect=side_effect),
            patch("hft_platform.bot.scheduler.asyncio") as mock_asyncio,
        ):
            mock_asyncio.sleep = AsyncMock()
            await _push_report(ctx, "day")

        assert bot_app.last_day_report is not None
