"""BotApp: initialise python-telegram-bot Application, register handlers, start polling."""

from __future__ import annotations

import asyncio
import functools
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

import structlog

from hft_platform.utils.logging import configure_logging

_log = structlog.get_logger(__name__)
_TZ = ZoneInfo("Asia/Taipei")


start_time: datetime = datetime.now(_TZ)
last_day_report: datetime | None = None
last_night_report: datetime | None = None
last_ch_ok: datetime | None = None

last_day_attempt: datetime | None = None
last_night_attempt: datetime | None = None
consecutive_empty_attempts: int = 0
DEAD_DATA_ALERT_THRESHOLD: int = 2


@dataclass(slots=True)
class LatestReportContext:
    symbol: str
    session: str
    date: str
    dossier: object
    decision: object | None


latest_manual_report_context: LatestReportContext | None = None


_OWNER_CHAT_ID: int = 0


def _get_owner_chat_id() -> int:
    global _OWNER_CHAT_ID
    if _OWNER_CHAT_ID == 0:
        raw = os.environ.get("HFT_TELEGRAM_CHAT_ID", "")
        _OWNER_CHAT_ID = int(raw) if raw else 0
    return _OWNER_CHAT_ID


HandlerFunc = Callable[..., Coroutine[Any, Any, None]]


def owner_only(func: HandlerFunc) -> HandlerFunc:
    """Decorator that restricts handler to the configured owner chat_id."""

    @functools.wraps(func)
    async def wrapper(update: Any, context: Any) -> None:
        chat_id = update.effective_chat.id
        if chat_id != _get_owner_chat_id():
            _log.warning("bot.unauthorized", chat_id=chat_id)
            await update.message.reply_text("未授權")
            return
        await func(update, context)

    return wrapper


def get_report_symbols() -> list[str]:
    """Return the list of symbols to include in reports.

    Reads ``HFT_REPORT_SYMBOLS`` (comma-separated). Falls back to
    ``["TXFR1"]`` when absent or empty.  R1 = Shioaji continuous front-month.
    """
    raw = os.environ.get("HFT_REPORT_SYMBOLS", "TXFR1")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        symbols = ["TXFR1"]
    return symbols


def _start_health_server_background() -> None:
    """Expose a lightweight /healthz endpoint for container liveness probes."""
    from hft_platform.observability.health import HealthServer

    health = HealthServer(system=None)
    thread = threading.Thread(
        target=lambda: asyncio.run(health.run()),
        name="bot-health-server",
        daemon=True,
    )
    thread.start()


async def _telegram_error_handler(update: Any, context: Any) -> None:
    """P1-c (2026-04-27): catch otherwise-uncaught handler exceptions.

    python-telegram-bot Application logs `No error handlers are registered`
    when an exception escapes a handler. The most common case is
    ``httpx.ConnectError`` during transient network outages — these were
    previously spamming the log AND likely contributing to the bot wedging.

    This handler:
      * Logs via structlog (which has the credential scrubber pipeline),
      * Increments a Prometheus counter labeled by exception class,
      * Returns normally so the bot continues polling.
    """
    err = context.error
    err_type = type(err).__name__ if err is not None else "Unknown"
    _log.warning(
        "bot.handler_error",
        error_type=err_type,
        error=str(err) if err is not None else "",
        update_kind=type(update).__name__ if update is not None else "None",
    )
    try:
        from hft_platform.observability.metrics import MetricsRegistry

        MetricsRegistry.get().bot_handler_errors_total.labels(exception=err_type).inc()
    except Exception:
        pass


def create_app() -> Any:
    """Build and return a configured telegram.ext.Application."""
    from telegram.ext import Application, CommandHandler

    from hft_platform.bot.handlers import (
        cmd_ask,
        cmd_flow,
        cmd_levels,
        cmd_pos,
        cmd_report,
        cmd_report_rule,
        cmd_start,
        cmd_status,
        cmd_stop,
    )
    from hft_platform.bot.scheduler import schedule_jobs

    token = os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("HFT_TELEGRAM_BOT_TOKEN is required")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("report_rule", cmd_report_rule))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("flow", cmd_flow))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pos", cmd_pos))
    app.add_handler(CommandHandler("stop", cmd_stop))

    app.add_error_handler(_telegram_error_handler)

    schedule_jobs(app.job_queue)

    _log.info("bot.app_created")
    return app


def main() -> None:
    """Entry point: create app and start polling."""
    global start_time
    start_time = datetime.now(_TZ)

    configure_logging()

    _start_health_server_background()
    app = create_app()
    _log.info("bot.started")
    app.run_polling(drop_pending_updates=True)
