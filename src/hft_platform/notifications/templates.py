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


def render_autonomy_transition(
    *,
    scope: str,
    from_mode: str,
    to_mode: str,
    reason: str,
) -> str:
    """Autonomy state transition notification.

    Args:
        scope: "platform" or "strategy".
        from_mode: Previous autonomy mode name.
        to_mode: New autonomy mode name.
        reason: Human-readable reason for the transition.

    Returns:
        Formatted autonomy transition alert string.
    """
    return f"🟠 Autonomy: [{scope}] {from_mode} → {to_mode}. Reason: {reason}."


def render_flatten_result(
    *,
    scope: str,
    fully_closed: int,
    partially_closed: int,
    failed: int,
    failed_symbols: list[str],
) -> str:
    """Position flattening result notification.

    Args:
        scope: "all", "track", or strategy id.
        fully_closed: Number of positions fully closed.
        partially_closed: Number of positions partially closed.
        failed: Number of positions that failed to close.
        failed_symbols: Symbols that failed to flatten.

    Returns:
        Formatted flatten result string.
    """
    icon = "✅" if failed == 0 else "⚠️"
    lines = [
        f"{icon} Flatten [{scope}]: closed={fully_closed} partial={partially_closed} failed={failed}",
    ]
    if failed_symbols:
        lines.append(f"Failed: {', '.join(failed_symbols[:5])}")
    return "\n".join(lines)


def render_heartbeat(
    *,
    autonomy_state: str,
    pnl_scaled: int,
    strategies_active: int,
    feed_status: str,
) -> str:
    """Periodic heartbeat notification.

    Args:
        autonomy_state: Current StormGuard state name.
        pnl_scaled: Current PnL (scaled int).
        strategies_active: Number of active strategies.
        feed_status: "ok" or "disconnected".

    Returns:
        Formatted heartbeat string.
    """
    return (
        f"💓 Heartbeat | State: {autonomy_state} | "
        f"PnL: {pnl_scaled} | Strategies: {strategies_active} | Feed: {feed_status}"
    )


def render_session_phase(
    *,
    track: str,
    old_phase: str,
    new_phase: str,
) -> str:
    """Session phase transition notification.

    Args:
        track: Track name (e.g. "stock", "futures_day").
        old_phase: Previous session phase name.
        new_phase: New session phase name.

    Returns:
        Formatted session phase change string.
    """
    return f"🕐 Session [{track}]: {old_phase} → {new_phase}"


def render_autonomy_daily_summary(
    *,
    date_str: str,
    transitions: int,
    halts: int,
    flatten_count: int,
    manual_rearms: int,
) -> str:
    """End-of-day autonomy summary notification.

    Args:
        date_str: Date label, e.g. "2026-03-25".
        transitions: Total autonomy transitions for the day.
        halts: Number of HALT events.
        flatten_count: Number of flatten operations triggered.
        manual_rearms: Number of manual rearm operations performed.

    Returns:
        Formatted autonomy daily summary string.
    """
    return (
        f"📋 Autonomy 日報 {date_str}\n"
        f"Transitions: {transitions} | HALTs: {halts}\n"
        f"Flatten ops: {flatten_count} | Manual rearms: {manual_rearms}"
    )


def render_shadow_daily_report(
    *,
    date_str: str,
    intent_count: int,
    buys: int,
    sells: int,
    simulated_pnl_ntd: int,
    latency_p50_ms: float,
    latency_p95_ms: float,
    latency_p99_ms: float,
    reconnect_count: int,
    queue_peak_pct: int,
    rss_gb: float,
    storm_guard_state: str,
) -> str:
    """Shadow strategy daily report for solo operator.

    Args:
        date_str: Date label, e.g. "2026-04-22 (二)".
        intent_count: Total number of OrderIntents generated during the day.
        buys: Number of buy-side OrderIntents.
        sells: Number of sell-side OrderIntents.
        simulated_pnl_ntd: Simulated PnL for the day in NTD (with 1-tick slippage).
        latency_p50_ms: P50 tick-to-signal latency in milliseconds.
        latency_p95_ms: P95 tick-to-signal latency in milliseconds.
        latency_p99_ms: P99 tick-to-signal latency in milliseconds.
        reconnect_count: Number of broker reconnects during the session.
        queue_peak_pct: Peak queue depth as percentage of capacity.
        rss_gb: Current RSS memory usage in GB.
        storm_guard_state: Final StormGuard FSM state name.

    Returns:
        Multi-line formatted shadow daily report string.
    """
    sign = "+" if simulated_pnl_ntd >= 0 else ""
    return (
        f"📊 Shadow 日報 {date_str}\n\n"
        f"🔮 信號: {intent_count} OrderIntents (買 {buys} / 賣 {sells})\n"
        f"💰 模擬 PnL: {sign}{simulated_pnl_ntd:,} NTD (含 1-tick slippage)\n\n"
        f"⏱ 延遲:\n"
        f"  tick→signal P50: {latency_p50_ms:.1f}ms / P95: {latency_p95_ms:.1f}ms / P99: {latency_p99_ms:.1f}ms\n\n"
        f"📈 系統:\n"
        f"  Reconnect: {reconnect_count} / Queue peak: {queue_peak_pct}% / RSS: {rss_gb:.1f} GB\n"
        f"  StormGuard: {storm_guard_state} (全日)"
    )


def render_backup_success(
    *,
    date_str: str,
    size_mb: float,
    duration_s: float,
    retained_count: int,
) -> str:
    """Daily ClickHouse backup completed successfully.

    Args:
        date_str: Date label, e.g. "2026-03-25".
        size_mb: Backup size in megabytes.
        duration_s: Backup duration in seconds.
        retained_count: Number of backups currently retained on disk.

    Returns:
        Formatted backup success notification string.
    """
    return (
        f"🟢 Backup {date_str} 完成\n"
        f"大小: {size_mb:,.1f} MB | 耗時: {duration_s:.1f}s\n"
        f"保留: {retained_count} 份備份"
    )


def render_backup_failed(
    *,
    date_str: str,
    error: str,
    last_success_date: str,
) -> str:
    """Daily ClickHouse backup failed.

    Args:
        date_str: Date label, e.g. "2026-03-25".
        error: Error message describing the failure.
        last_success_date: Date of the last successful backup, e.g. "2026-03-24".

    Returns:
        Formatted backup failure notification string.
    """
    return (
        f"🔴 BACKUP 失敗 {date_str}\n"
        f"錯誤: {error}\n"
        f"最後成功備份: {last_success_date}\n"
        f"請立即檢查備份磁碟"
    )
