# src/hft_platform/bot/handlers.py
"""Command handlers for the Telegram Bot."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from hft_platform.bot.app import owner_only

_log = structlog.get_logger(__name__)
_TZ = ZoneInfo("Asia/Taipei")

PLATFORM_SCALE = 10_000


def _parse_report_args(args: list[str]) -> tuple[str, str | None]:
    """Parse /report [symbol] [day|night] positional args.

    Returns (symbol, session_or_none).
    - No args: (default_symbol, None)
    - One arg: if 'day'/'night' → (default_symbol, session); else → (arg, None)
    - Two args: (symbol, session)
    """
    from hft_platform.bot.app import get_report_symbols

    default_symbol = get_report_symbols()[0]
    sessions = {"day", "night"}

    if not args:
        return default_symbol, None
    if len(args) == 1:
        if args[0].lower() in sessions:
            return default_symbol, args[0].lower()
        return args[0].upper(), None
    # Two or more args: first = symbol, second = session
    symbol = args[0].upper()
    session = args[1].lower() if args[1].lower() in sessions else None
    return symbol, session


def _prev_trading_date(now: datetime) -> str:
    """Return the most recent trading day (skip Sat/Sun) as YYYY-MM-DD."""
    d = now.astimezone(_TZ)
    # weekday: 0=Mon .. 6=Sun
    if d.weekday() == 5:  # Saturday → Friday
        d -= timedelta(days=1)
    elif d.weekday() == 6:  # Sunday → Friday
        d -= timedelta(days=2)
    return d.strftime("%Y-%m-%d")


def _collect_core_for_latest() -> Any:
    """Run collect_core() for the most recent session, skipping weekends.

    Tries up to 5 days back to find a session with data.
    """
    from hft_platform.bot.app import get_report_symbols
    from hft_platform.reports.collector import DataCollector, _day_filter, _night_filter

    now = datetime.now(_TZ)
    symbol = get_report_symbols()[0]
    collector = DataCollector()
    sd = None

    for days_back in range(5):
        check_time = now - timedelta(days=days_back)
        date = _prev_trading_date(check_time)

        for session in ("day", "night"):
            time_filter = _day_filter(date) if session == "day" else _night_filter(date)
            sd = collector.collect_core(symbol, time_filter, session=session, date=date)
            if sd.tick_count > 0:
                return sd

    return sd  # Return last attempt even if empty


@owner_only
async def cmd_start(update: Any, context: Any) -> None:
    """Handle /start command."""
    text = (
        "HFT 市場分析 Bot\n\n"
        "可用指令：\n"
        "/report [day|night] — 取得完整分析報告\n"
        "/levels — 當前支撐壓力位\n"
        "/flow — 最新流向摘要\n"
        "/status — Bot 運行狀態"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_report(update: Any, context: Any) -> None:
    """Handle /report [symbol] [day|night] command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.pipeline import build_report, resolve_trading_date

    symbol, session = _parse_report_args(context.args or [])
    if session is None:
        now = datetime.now(_TZ)
        session = "day" if 7 <= now.hour < 15 else "night"

    date = resolve_trading_date(session)
    await update.message.reply_text(f"產生報告中... ({symbol} {session} {date})")

    try:
        rendered = build_report(session, date, symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.report_error", exc=str(exc), exc_info=True)
        await update.message.reply_text(f"報告產生失敗：{exc}")
        return

    if rendered is None:
        await update.message.reply_text("該時段無交易資料")
        return

    chat_id = update.effective_chat.id
    for msg in rendered["paid"]:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
        await asyncio.sleep(1.5)

    if session == "day":
        bot_app.last_day_report = datetime.now(_TZ)
    else:
        bot_app.last_night_report = datetime.now(_TZ)


@owner_only
async def cmd_levels(update: Any, context: Any) -> None:
    """Handle /levels command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.signals import SignalEngine

    try:
        sd = _collect_core_for_latest()
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.levels_error", exc=str(exc), exc_info=True)
        await update.message.reply_text("資料庫暫時不可用，請稍後再試")
        return

    if sd.tick_count == 0:
        await update.message.reply_text("該時段無交易資料")
        return

    engine = SignalEngine()
    signal = engine.analyze(sd)

    session_label = "日盤" if sd.session == "day" else "夜盤"
    lines = [f"支撐壓力位 ({sd.symbol} {session_label} {sd.date})\n"]

    if signal.resistances:
        lines.append("壓力：")
        for i, r in enumerate(signal.resistances, 1):
            stars = "★" * max(1, int(r.strength * 3))
            lines.append(f"  R{i}: {r.price // PLATFORM_SCALE:,} {stars} {r.reason}")

    if signal.supports:
        lines.append("\n支撐：")
        for i, s in enumerate(signal.supports, 1):
            stars = "★" * max(1, int(s.strength * 3))
            lines.append(f"  S{i}: {s.price // PLATFORM_SCALE:,} {stars} {s.reason}")

    if not signal.resistances and not signal.supports:
        lines.append("（未偵測到顯著支撐壓力位）")

    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_flow(update: Any, context: Any) -> None:
    """Handle /flow command."""
    import hft_platform.bot.app as bot_app

    try:
        sd = _collect_core_for_latest()
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.flow_error", exc=str(exc), exc_info=True)
        await update.message.reply_text("資料庫暫時不可用，請稍後再試")
        return

    if sd.tick_count == 0:
        await update.message.reply_text("該時段無交易資料")
        return

    session_label = "日盤" if sd.session == "day" else "夜盤"

    total_up = sum(f.uptick_vol for f in sd.flow_5m)
    total_dn = sum(f.downtick_vol for f in sd.flow_5m)
    ud_ratio = total_up / total_dn if total_dn > 0 else (float(total_up) if total_up > 0 else 1.0)

    if ud_ratio >= 1.1:
        bias_label = "偏多"
    elif ud_ratio <= 0.9:
        bias_label = "偏空"
    else:
        bias_label = "中性"

    buy_trades = sum(1 for t in sd.large_trades if t.direction == "buy")
    sell_trades = sum(1 for t in sd.large_trades if t.direction == "sell")

    lines = [
        f"流向摘要 ({sd.symbol} {session_label} {sd.date})\n",
        f"U/D Ratio: {ud_ratio:.2f} ({bias_label})",
        f"成交量: {sd.volume:,}",
        f"大單: 買 {buy_trades} 筆 / 賣 {sell_trades} 筆",
    ]

    recent = sd.flow_5m[-5:] if sd.flow_5m else []
    if recent:
        lines.append("\n最近 5 根 K棒流向：")
        for bar in recent:
            arrow = "▲" if bar.ud_ratio >= 1.0 else "▼"
            lines.append(f"{bar.ts[-8:-3]} {arrow} {bar.ud_ratio:.2f}")

    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_status(update: Any, context: Any) -> None:
    """Handle /status command."""
    import hft_platform.bot.app as bot_app

    now = datetime.now(_TZ)
    uptime = now - bot_app.start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes = remainder // 60

    lines = [
        "Bot 狀態\n",
        f"運行時間: {hours}h {minutes}m",
    ]

    if bot_app.last_day_report:
        lines.append(f"上次日盤報告: {bot_app.last_day_report.strftime('%Y-%m-%d %H:%M')} CST")
    else:
        lines.append("上次日盤報告: —")

    if bot_app.last_night_report:
        lines.append(f"上次夜盤報告: {bot_app.last_night_report.strftime('%Y-%m-%d %H:%M')} CST")
    else:
        lines.append("上次夜盤報告: —")

    if bot_app.last_ch_ok:
        ch_ago = now - bot_app.last_ch_ok
        ch_mins = int(ch_ago.total_seconds()) // 60
        lines.append(f"ClickHouse: 最後成功 {ch_mins} 分鐘前")
    else:
        lines.append("ClickHouse: 尚未連線")

    await update.message.reply_text("\n".join(lines))
