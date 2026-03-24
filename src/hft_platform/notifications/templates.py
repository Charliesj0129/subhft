"""Structured message templates for Telegram solo-operator alerts.

All render functions return plain strings suitable for Telegram Bot API
text messages. No dynamic interpolation from untrusted input — all
parameters are typed and sanitised via Python's standard f-string
formatting directives.
"""

from __future__ import annotations


def render_halt(reason: str) -> str:
    """Critical: trading has been halted.

    Args:
        reason: Human-readable description of the halt cause.

    Returns:
        Formatted HALT alert string.
    """
    return f"🔴 HALT: {reason}. All trading stopped. Manual recovery required."


def render_daily_loss(pnl_ntd: int, limit_ntd: int) -> str:
    """Daily loss limit has been breached and HALT activated.

    Args:
        pnl_ntd: Current PnL in New Taiwan Dollars (negative = loss).
        limit_ntd: Configured daily loss limit in NTD (negative value).

    Returns:
        Formatted daily-loss alert string.
    """
    return f"🔴 日損限額觸及: PnL={pnl_ntd:,} NTD (limit={limit_ntd:,}). HALT activated."


def render_daily_report(
    *,
    date_str: str,
    pnl_ntd: int,
    buys: int,
    sells: int,
    fills: int,
    position_status: str,
    reconciliation_status: str,
    latency_p95_ms: float,
    reconnect_count: int,
    storm_guard_state: str,
    memory_gb: float,
    memory_max_gb: float,
) -> str:
    """End-of-day summary report for solo operator.

    Args:
        date_str: Date label, e.g. "2026-03-23".
        pnl_ntd: Realised PnL for the day in NTD.
        buys: Number of buy-side fills.
        sells: Number of sell-side fills.
        fills: Total fill count.
        position_status: Text description of end-of-day position, e.g. "FLAT".
        reconciliation_status: "OK" or mismatch description.
        latency_p95_ms: P95 strategy latency in milliseconds.
        reconnect_count: Number of broker reconnects during the session.
        storm_guard_state: Final StormGuard FSM state name.
        memory_gb: Current RSS memory usage in GB.
        memory_max_gb: Peak RSS memory usage in GB.

    Returns:
        Multi-line formatted daily report string.
    """
    sign = "+" if pnl_ntd >= 0 else ""
    return (
        f"📊 日報 {date_str}\n"
        f"PnL: {sign}{pnl_ntd:,} NTD\n"
        f"成交: {fills} fills (買 {buys} / 賣 {sells})\n"
        f"倉位: {position_status}\n"
        f"對帳: {reconciliation_status}\n"
        f"延遲 P95: {latency_p95_ms:.2f} ms\n"
        f"重連: {reconnect_count} 次\n"
        f"StormGuard: {storm_guard_state}\n"
        f"記憶體: {memory_gb:.2f} GB (峰值 {memory_max_gb:.2f} GB)"
    )


def render_stormguard_change(old: str, new: str, reason: str) -> str:
    """StormGuard FSM state transition notification.

    Args:
        old: Previous StormGuard state name.
        new: New StormGuard state name.
        reason: Human-readable reason for the transition.

    Returns:
        Formatted StormGuard state-change alert string.
    """
    return f"🟡 StormGuard: {old} → {new}. Reason: {reason}."


def render_pre_market_pass() -> str:
    """Pre-market health check passed; strategy will start soon.

    Returns:
        Formatted pre-market PASS notification string.
    """
    return "🟢 08:15 健檢 PASS. 策略將於 08:45 啟動."


def render_pre_market_fail(failed_checks: list[str]) -> str:
    """Pre-market health check failed; strategy will NOT start.

    Args:
        failed_checks: List of human-readable check descriptions that failed.

    Returns:
        Formatted pre-market FAIL notification string.
    """
    checks_formatted = "\n  • ".join(failed_checks)
    return f"🔴 08:15 健檢 FAIL. 策略不啟動.\n失敗項目:\n  • {checks_formatted}"


def render_reconciliation_mismatch(
    platform_pnl: int,
    broker_pnl: int,
    ch_pnl: int,
) -> str:
    """PnL reconciliation mismatch detected across data sources.

    Args:
        platform_pnl: PnL as reported by platform position tracker (NTD).
        broker_pnl: PnL as reported by broker account gateway (NTD).
        ch_pnl: PnL as stored in ClickHouse fills (NTD).

    Returns:
        Formatted reconciliation mismatch alert string.
    """
    return (
        f"⚠️ 對帳不符!\n"
        f"平台 PnL: {platform_pnl:,} NTD\n"
        f"券商 PnL: {broker_pnl:,} NTD\n"
        f"ClickHouse PnL: {ch_pnl:,} NTD\n"
        f"請立即核查成交紀錄."
    )


def render_reconnect_alert(count: int, flap_status: str) -> str:
    """Broker reconnect event notification.

    Args:
        count: Total reconnect count for the current session.
        flap_status: "OK", "FLAPPING", or similar descriptor from flap detector.

    Returns:
        Formatted reconnect alert string.
    """
    return f"🟡 券商重連 (第 {count} 次). Flap 狀態: {flap_status}."


def render_process_restart(attempt: int, max_attempts: int) -> str:
    """Supervisor is restarting a crashed subprocess.

    Args:
        attempt: Current restart attempt number (1-based).
        max_attempts: Maximum allowed restart attempts before giving up.

    Returns:
        Formatted process-restart alert string.
    """
    return f"🟡 Process restart attempt {attempt}/{max_attempts}. Supervisor restarting trading engine."


def render_weekly_summary(
    *,
    week_label: str,
    date_range: str,
    total_pnl_ntd: int,
    trading_days: int,
    avg_trades: float,
    best_day_ntd: int,
    worst_day_ntd: int,
    reconciliation_match: bool,
    halt_count: int,
    reconnect_count: int,
    latency_p95_avg_ms: float,
    rss_peak_gb: float,
    uptime_pct: float,
) -> str:
    """Weekly performance summary for solo operator.

    Args:
        week_label: Week identifier, e.g. "2026-W12".
        date_range: Human-readable date range, e.g. "2026-03-16 ~ 2026-03-20".
        total_pnl_ntd: Cumulative realised PnL for the week in NTD.
        trading_days: Number of days with active trading sessions.
        avg_trades: Average number of fills per trading day.
        best_day_ntd: Best single-day PnL in NTD.
        worst_day_ntd: Worst single-day PnL in NTD.
        reconciliation_match: True if all daily reconciliations passed.
        halt_count: Number of HALT events triggered during the week.
        reconnect_count: Total broker reconnects during the week.
        latency_p95_avg_ms: Average P95 strategy latency over the week (ms).
        rss_peak_gb: Peak RSS memory usage observed during the week (GB).
        uptime_pct: System uptime percentage over the trading week.

    Returns:
        Multi-line formatted weekly summary string.
    """
    sign = "+" if total_pnl_ntd >= 0 else ""
    recon_icon = "✅" if reconciliation_match else "❌"
    halt_icon = "✅" if halt_count == 0 else "⚠️"
    return (
        f"📈 週報 {week_label} ({date_range})\n"
        f"總 PnL: {sign}{total_pnl_ntd:,} NTD\n"
        f"交易日: {trading_days} 天 | 日均成交: {avg_trades:.1f} 次\n"
        f"最佳日: {best_day_ntd:+,} NTD\n"
        f"最差日: {worst_day_ntd:+,} NTD\n"
        f"對帳: {recon_icon} {'全數吻合' if reconciliation_match else '有不符'}\n"
        f"HALT: {halt_icon} {halt_count} 次\n"
        f"重連: {reconnect_count} 次\n"
        f"延遲 P95 均值: {latency_p95_avg_ms:.2f} ms\n"
        f"RSS 峰值: {rss_peak_gb:.2f} GB\n"
        f"可用率: {uptime_pct:.1f}%"
    )
