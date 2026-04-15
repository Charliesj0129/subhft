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

_log = structlog.get_logger(__name__)
_TZ = ZoneInfo("Asia/Taipei")

# ---------------------------------------------------------------------------
# Shared state (module-level, updated by handlers/scheduler)
# ---------------------------------------------------------------------------

start_time: datetime = datetime.now(_TZ)
last_day_report: datetime | None = None
last_night_report: datetime | None = None
last_ch_ok: datetime | None = None


@dataclass(slots=True)
class LatestReportContext:
    symbol: str
    session: str
    date: str
    dossier: object
    decision: object | None


latest_manual_report_context: LatestReportContext | None = None

# ---------------------------------------------------------------------------
# Owner-only access control
# ---------------------------------------------------------------------------

_OWNER_CHAT_ID: int = 0


def _get_owner_chat_id() -> int:
    global _OWNER_CHAT_ID  # noqa: PLW0603
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


# ---------------------------------------------------------------------------
# Symbol configuration
# ---------------------------------------------------------------------------


def get_report_symbols() -> list[str]:
    """Return the list of symbols to include in reports.

    Reads ``HFT_REPORT_SYMBOLS`` (comma-separated). Falls back to
    ``["TXFC0"]`` when absent or empty.  C0 = front-month alias.
    """
    raw = os.environ.get("HFT_REPORT_SYMBOLS", "TXFC0")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        symbols = ["TXFC0"]
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


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> Any:
    """Build and return a configured telegram.ext.Application."""
    from telegram.ext import Application, CommandHandler

    from hft_platform.bot.handlers import (
        cmd_ask,
        cmd_flow,
        cmd_levels,
        cmd_report,
        cmd_report_rule,
        cmd_start,
        cmd_status,
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

    schedule_jobs(app.job_queue)

    _log.info("bot.app_created")
    return app


def main() -> None:
    """Entry point: create app and start polling."""
    global start_time  # noqa: PLW0603
    start_time = datetime.now(_TZ)

    _start_health_server_background()
    app = create_app()
    _log.info("bot.started")
    app.run_polling(drop_pending_updates=True)
