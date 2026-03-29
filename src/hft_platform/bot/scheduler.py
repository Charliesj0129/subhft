"""Scheduled push jobs for the Telegram Bot."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import structlog

_log = structlog.get_logger(__name__)
_TZ = ZoneInfo("Asia/Taipei")


def _get_owner_chat_id() -> str:
    return os.environ.get("HFT_TELEGRAM_CHAT_ID", "")


async def _push_report(context: Any, session: str) -> None:
    """Push reports for all configured symbols."""
    import hft_platform.bot.app as bot_app
    from hft_platform.bot.app import get_report_symbols
    from hft_platform.reports.pipeline import build_report, resolve_trading_date

    chat_id = _get_owner_chat_id()
    if not chat_id:
        _log.error("bot.push_no_chat_id")
        return

    date = resolve_trading_date(session)
    symbols = get_report_symbols()
    _log.info("bot.push_start", session=session, date=date, symbols=symbols)

    sent_any = False
    for symbol in symbols:
        try:
            rendered = build_report(session, date, symbol)
            bot_app.last_ch_ok = datetime.now(_TZ)
        except Exception as exc:
            _log.error("bot.push_error", session=session, symbol=symbol, exc=str(exc), exc_info=True)
            continue

        if rendered is None:
            _log.info("bot.push_no_data", session=session, date=date, symbol=symbol)
            continue

        for msg in rendered["paid"]:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
            await asyncio.sleep(1.5)
        sent_any = True

    if sent_any:
        now = datetime.now(_TZ)
        if session == "day":
            bot_app.last_day_report = now
        else:
            bot_app.last_night_report = now

    _log.info("bot.push_complete", session=session, date=date, symbols=len(symbols))


async def _push_day(context: Any) -> None:
    await _push_report(context, "day")


async def _push_night(context: Any) -> None:
    await _push_report(context, "night")


async def _heartbeat(context: Any) -> None:
    """Log heartbeat with uptime and last report timestamps."""
    import hft_platform.bot.app as bot_app

    now = datetime.now(_TZ)
    uptime_s = int((now - bot_app.start_time).total_seconds())
    _log.info(
        "bot.heartbeat",
        uptime_s=uptime_s,
        last_day=str(bot_app.last_day_report),
        last_night=str(bot_app.last_night_report),
    )


def schedule_jobs(job_queue: Any) -> None:
    """Register scheduled jobs on the JobQueue."""
    # Day report: 13:50 CST, Mon-Fri
    job_queue.run_daily(
        _push_day,
        time=time(hour=13, minute=50, tzinfo=_TZ),
        days=(0, 1, 2, 3, 4),
        name="push_day_report",
    )

    # Night report: 05:05 CST, Mon-Sat
    job_queue.run_daily(
        _push_night,
        time=time(hour=5, minute=5, tzinfo=_TZ),
        days=(0, 1, 2, 3, 4, 5),
        name="push_night_report",
    )

    # Heartbeat: every 5 minutes
    job_queue.run_repeating(
        _heartbeat,
        interval=300,
        name="heartbeat",
    )

    _log.info("bot.jobs_scheduled")
