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

# ---------------------------------------------------------------------------
# Shared state (module-level, updated by handlers/scheduler)
# ---------------------------------------------------------------------------

start_time: datetime = datetime.now(_TZ)
last_day_report: datetime | None = None
last_night_report: datetime | None = None
last_ch_ok: datetime | None = None

# P1-c (2026-04-27): track ATTEMPTS separately from successes so a heartbeat
# that says `last_day=None last_night=None` after 8.7 days isn't ambiguous —
# we can distinguish "scheduler never fired" from "scheduler fired but every
# symbol returned no_data". Used by scheduler.py.
last_day_attempt: datetime | None = None
last_night_attempt: datetime | None = None
consecutive_empty_attempts: int = 0
DEAD_DATA_ALERT_THRESHOLD: int = 2  # warn after this many consecutive empty pushes


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


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


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
    except Exception:  # noqa: BLE001 — never crash the error handler itself
        pass


# ---------------------------------------------------------------------------
# Alertmanager -> Telegram bridge
#
# Runs here, not in the trading engine: when hft-engine is stopped or down the
# bridge inside it is down too, so Alertmanager cannot deliver HFTEngineDown
# (2026-11-11: 18 notifications given up with "lookup hft-engine"). This process
# is always on. Alertmanager's webhook url must point at hft-bot:8081.
# ---------------------------------------------------------------------------

_ALERTMANAGER_BRIDGE_ENV = "HFT_ALERTMANAGER_BRIDGE_ENABLED"
_BRIDGE_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

# Strong references: the event loop only keeps weak references to tasks, so a
# bridge task nobody holds can be garbage-collected mid-flight.
_alertmanager_bridge: Any = None
_alertmanager_bridge_task: asyncio.Task[None] | None = None


def _alertmanager_bridge_enabled() -> bool:
    """Env kill-switch: ``HFT_ALERTMANAGER_BRIDGE_ENABLED=0`` leaves the bridge off."""
    raw = os.environ.get(_ALERTMANAGER_BRIDGE_ENV, "1").strip().lower()
    return raw not in _BRIDGE_DISABLED_VALUES


def _on_bridge_task_done(task: asyncio.Task[None]) -> None:
    """A bridge that stops on its own (bind failure, crash) must not do so silently."""
    # run() swallows CancelledError, so a shutdown-driven stop ends "normally";
    # _post_shutdown drops the module reference first, which marks it expected.
    if task.cancelled() or task is not _alertmanager_bridge_task:
        return
    exc = task.exception()
    _log.warning(
        "alertmanager_bridge_task_ended_in_bot",
        error=repr(exc) if exc is not None else "run() returned (see alertmanager_bridge_bind_failed)",
    )


def _start_alertmanager_bridge() -> asyncio.Task[None] | None:
    """Schedule ``AlertmanagerBridge().run()`` on the running loop.

    Never raises: a bridge that cannot start costs the Alertmanager relay, not
    the bot's command handlers and scheduled reports.
    """
    global _alertmanager_bridge, _alertmanager_bridge_task  # noqa: PLW0603
    if not _alertmanager_bridge_enabled():
        _log.info("alertmanager_bridge_disabled_in_bot", env=_ALERTMANAGER_BRIDGE_ENV)
        return None
    try:
        from hft_platform.notifications.alertmanager_bridge import AlertmanagerBridge

        bridge = AlertmanagerBridge()
        task = asyncio.get_running_loop().create_task(bridge.run(), name="alertmanager-bridge")
    except Exception:  # noqa: BLE001 — bridge failure must not stop the bot
        _log.warning("alertmanager_bridge_start_failed_in_bot", exc_info=True)
        return None
    task.add_done_callback(_on_bridge_task_done)
    _alertmanager_bridge = bridge
    _alertmanager_bridge_task = task
    _log.info("alertmanager_bridge_started_in_bot")
    return task


async def _post_init(application: Any) -> None:
    """PTB ``post_init`` hook: runs on the loop ``run_polling`` owns."""
    _start_alertmanager_bridge()


async def _post_shutdown(application: Any) -> None:
    """PTB ``post_shutdown`` hook: stop the bridge and release its Telegram session."""
    global _alertmanager_bridge, _alertmanager_bridge_task  # noqa: PLW0603
    bridge, task = _alertmanager_bridge, _alertmanager_bridge_task
    _alertmanager_bridge = None
    _alertmanager_bridge_task = None
    if task is not None and not task.done():
        task.cancel()
        # Let the cancellation run before PTB closes the loop: otherwise the
        # task is destroyed pending and run()'s cleanup never logs.
        await asyncio.wait({task}, timeout=5.0)
    if bridge is not None:
        try:
            await bridge.close()
        except Exception:  # noqa: BLE001 — shutdown must finish
            _log.warning("alertmanager_bridge_close_failed_in_bot", exc_info=True)


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

    # post_init runs on the loop run_polling() owns; it starts the Alertmanager
    # bridge there. create_app() had no post_init before, so nothing to chain.
    app = Application.builder().token(token).post_init(_post_init).post_shutdown(_post_shutdown).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("report_rule", cmd_report_rule))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("flow", cmd_flow))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pos", cmd_pos))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # P1-c: register error handler so httpx.ConnectError etc. are caught,
    # logged via structlog (with secret scrubber), and counted in metrics
    # instead of polluting logs and risking wedge state.
    app.add_error_handler(_telegram_error_handler)

    schedule_jobs(app.job_queue)

    _log.info("bot.app_created")
    return app


def main() -> None:
    """Entry point: create app and start polling."""
    global start_time  # noqa: PLW0603
    start_time = datetime.now(_TZ)

    # This process is its own entry point (`python -m hft_platform.bot`) and used to
    # run on structlog's DEFAULT chain: no credential scrubber, and python-telegram-bot
    # (which carries the bot token in every request URL) logging through stdlib with
    # no scrubbing at all. The comments below claim a scrubber; this call is what makes
    # that true. It must run before anything can log, so before the health server.
    configure_logging()

    _start_health_server_background()
    app = create_app()
    _log.info("bot.started")
    app.run_polling(drop_pending_updates=True)
