# src/hft_platform/bot/handlers.py
"""Command handlers for the Telegram Bot."""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from hft_platform.bot.app import owner_only
from hft_platform.reports.models import ComposedReport

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


def _collect_core_for_latest(symbol: str | None = None) -> Any:
    """Run collect_core() for the most recent session, skipping weekends.

    Tries up to 5 days back to find a session with data.
    Deduplicates dates so that e.g. Saturday and Friday-offset both pointing
    to the same Friday date do not trigger redundant ClickHouse queries.
    """
    from hft_platform.bot.app import get_report_symbols
    from hft_platform.reports.collector import DataCollector, _day_filter, _night_filter

    now = datetime.now(_TZ)
    if symbol is None:
        symbol = get_report_symbols()[0]
    collector = DataCollector()
    sd = None
    seen_dates: set[str] = set()

    for days_back in range(5):
        check_time = now - timedelta(days=days_back)
        date = _prev_trading_date(check_time)
        if date in seen_dates:
            continue
        seen_dates.add(date)

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
        "/report [symbol] [day|night] — Hybrid LLM 報告\n"
        "/report_rule [symbol] [day|night] — 規則版報告\n"
        "/ask <問題> — 追問最近一次 manual hybrid 報告\n"
        "/levels [symbol] — 支撐壓力位\n"
        "/flow [symbol] — 流向摘要\n"
        "/status — Bot 運行狀態\n\n"
        "symbol 可省略，預設使用第一個設定商品"
    )
    await update.message.reply_text(text)


@owner_only
async def cmd_report(update: Any, context: Any) -> None:
    """Handle /report [symbol] [day|night] command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.pipeline import build_hybrid_report_async, resolve_trading_date

    symbol, session = _parse_report_args(context.args or [])
    if session is None:
        now = datetime.now(_TZ)
        session = "day" if 7 <= now.hour < 15 else "night"

    date = resolve_trading_date(session)
    await update.message.reply_text(f"產生報告中... ({symbol} {session} {date})")
    bot_app.latest_manual_report_context = None

    try:
        result = await build_hybrid_report_async(session, date, symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.report_error", exc=str(exc), exc_info=True)
        await update.message.reply_text(f"報告產生失敗：{exc}")
        return

    if result.composed is None:
        await update.message.reply_text("該時段無交易資料")
        return

    if result.decision is not None and result.dossier is not None:
        bot_app.latest_manual_report_context = bot_app.LatestReportContext(
            symbol=result.dossier.symbol,
            session=result.dossier.session,
            date=result.dossier.date,
            dossier=result.dossier,
            decision=result.decision,
        )

    await _send_composed(update, context, result.composed)

    if session == "day":
        bot_app.last_day_report = datetime.now(_TZ)
    else:
        bot_app.last_night_report = datetime.now(_TZ)


@owner_only
async def cmd_report_rule(update: Any, context: Any) -> None:
    """Handle /report_rule [symbol] [day|night] command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.pipeline import build_report, resolve_trading_date

    symbol, session = _parse_report_args(context.args or [])
    if session is None:
        now = datetime.now(_TZ)
        session = "day" if 7 <= now.hour < 15 else "night"

    date = resolve_trading_date(session)
    await update.message.reply_text(f"產生規則版報告中... ({symbol} {session} {date})")

    try:
        composed = await asyncio.to_thread(build_report, session, date, symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.report_rule_error", exc=str(exc), exc_info=True)
        await update.message.reply_text(f"報告產生失敗：{exc}")
        return

    if composed is None:
        await update.message.reply_text("該時段無交易資料")
        return

    await _send_composed(update, context, composed)

    if session == "day":
        bot_app.last_day_report = datetime.now(_TZ)
    else:
        bot_app.last_night_report = datetime.now(_TZ)


@owner_only
async def cmd_ask(update: Any, context: Any) -> None:
    """Handle /ask <question> for the latest manual hybrid report."""
    import hft_platform.bot.app as bot_app

    latest_context = bot_app.latest_manual_report_context
    if latest_context is None:
        await update.message.reply_text("目前沒有可追問的 hybrid 報告，請先執行 /report")
        return
    if latest_context.decision is None:
        await update.message.reply_text("最近一次 /report 沒有成功產生 LLM 判讀，請先重新執行 /report")
        return

    question = " ".join(context.args or []).strip()
    if not question:
        await update.message.reply_text("用法：/ask <問題>")
        return

    try:
        from hft_platform.reports.llm_reasoner import answer_followup_question
    except ImportError:
        await update.message.reply_text("追問功能尚未啟用")
        return

    answer = await answer_followup_question(latest_context, question)
    await update.message.reply_text(answer, parse_mode="HTML")


async def _send_composed(update: Any, context: Any, composed: ComposedReport) -> None:
    """Send a composed report to the current chat with Telegram-safe pacing."""
    chat_id = update.effective_chat.id
    for i, part in enumerate(composed.messages):
        if part.kind == "text":
            await context.bot.send_message(chat_id=chat_id, text=part.content, parse_mode="HTML")
        elif part.kind == "image" and part.image is not None:
            await context.bot.send_photo(chat_id=chat_id, photo=io.BytesIO(part.image), caption=part.caption)
        if i < len(composed.messages) - 1:
            await asyncio.sleep(1.5)


@owner_only
async def cmd_levels(update: Any, context: Any) -> None:
    """Handle /levels command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.facts import extract_all
    from hft_platform.reports.reasoner import LevelReasoner

    args = context.args or []
    symbol = args[0].upper() if args else None

    try:
        sd = _collect_core_for_latest(symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.levels_error", exc=str(exc), exc_info=True)
        await update.message.reply_text("資料庫暫時不可用，請稍後再試")
        return

    if sd.tick_count == 0:
        await update.message.reply_text("該時段無交易資料")
        return

    fr = extract_all(sd, prev_days=[])
    levels = LevelReasoner().analyze(fr)

    session_label = "日盤" if sd.session == "day" else "夜盤"
    lines = [f"支撐壓力位 ({sd.symbol} {session_label} {sd.date})\n"]

    resistances = [lv for lv in levels if lv.side == "resistance"]
    supports = [lv for lv in levels if lv.side == "support"]

    if resistances:
        lines.append("壓力：")
        for i, r in enumerate(resistances, 1):
            stars = "★" * max(1, int(r.strength * 3))
            sources_str = ", ".join(r.sources)
            lines.append(f"  R{i}: {r.price // PLATFORM_SCALE:,} {stars} [{sources_str}]")

    if supports:
        lines.append("\n支撐：")
        for i, s in enumerate(supports, 1):
            stars = "★" * max(1, int(s.strength * 3))
            sources_str = ", ".join(s.sources)
            lines.append(f"  S{i}: {s.price // PLATFORM_SCALE:,} {stars} [{sources_str}]")

    if not resistances and not supports:
        lines.append("（未偵測到顯著支撐壓力位）")

    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_flow(update: Any, context: Any) -> None:
    """Handle /flow command."""
    import hft_platform.bot.app as bot_app
    from hft_platform.reports.facts import extract_all

    args = context.args or []
    symbol = args[0].upper() if args else None

    try:
        sd = _collect_core_for_latest(symbol)
        bot_app.last_ch_ok = datetime.now(_TZ)
    except Exception as exc:
        _log.error("bot.flow_error", exc=str(exc), exc_info=True)
        await update.message.reply_text("資料庫暫時不可用，請稍後再試")
        return

    if sd.tick_count == 0:
        await update.message.reply_text("該時段無交易資料")
        return

    fr = extract_all(sd, prev_days=[])
    flow = fr.flow

    session_label = "日盤" if sd.session == "day" else "夜盤"

    if flow.session_ud >= 1.1:
        bias_label = "偏多"
    elif flow.session_ud <= 0.9:
        bias_label = "偏空"
    else:
        bias_label = "中性"

    lines = [
        f"流向摘要 ({sd.symbol} {session_label} {sd.date})\n",
        f"U/D Ratio: {flow.session_ud:.2f} ({bias_label})",
        f"Net Flow: {flow.session_net_flow:+,}",
        f"成交量: {sd.volume:,}",
    ]

    # Strongest bars
    lines.append(f"\n最強買: {flow.strongest_buy_bar.ts[-8:-3]} UD={flow.strongest_buy_bar.ud_ratio:.2f}")
    lines.append(f"最強賣: {flow.strongest_sell_bar.ts[-8:-3]} UD={flow.strongest_sell_bar.ud_ratio:.2f}")

    # Segment summaries
    if fr.segments:
        lines.append("\n時段摘要：")
        for seg in fr.segments:
            side_icon = "▲" if seg.dominant_side == "bull" else ("▼" if seg.dominant_side == "bear" else "─")
            lines.append(f"  {seg.name}: {side_icon} UD={seg.ud_ratio:.2f} vol={seg.volume:,}")

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
