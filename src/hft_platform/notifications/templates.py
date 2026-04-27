"""Structured message templates for Telegram solo-operator alerts.

All render functions return plain strings suitable for Telegram Bot API
text messages. No dynamic interpolation from untrusted input — all
parameters are typed and sanitised via Python's standard f-string
formatting directives.
"""

from __future__ import annotations

from html import escape


def render_halt(reason: str) -> str:
    """Critical: trading has been halted.

    Args:
        reason: Human-readable description of the halt cause.

    Returns:
        Formatted HALT alert string.
    """
    return f"🔴 HALT: {escape(reason)}. All trading stopped. Manual recovery required."


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
    return f"🟡 StormGuard: {escape(old)} → {escape(new)}. Reason: {escape(reason)}."


def render_pre_market_pass() -> str:
    """Pre-market health check passed; strategy will start soon.

    Returns:
        Formatted pre-market PASS notification string.
    """
    return "🟢 08:15 健檢 PASS. 策略將於 08:45 啟動."


def render_symbol_reload_failed(reason: str, count: int, limit: int) -> str:
    """Symbol config reload failed (RC-1 / Q3 fix).

    Args:
        reason: Result label (e.g. ``exceeds_limit``, ``parse_error``,
            ``other``).
        count: Number of symbols loaded from the config file (or 0 if
            the file could not be parsed).
        limit: Effective preflight ceiling at the moment of failure.

    Returns:
        Formatted symbol-reload failure notification string.
    """
    return (
        "🔴 Symbol reload FAIL\n"
        f"reason: {reason}\n"
        f"count: {count}\n"
        f"limit: {limit}\n"
        "Subscriptions are stale until reload succeeds — check "
        "config/symbols.yaml and HFT_MAX_SUBSCRIPTIONS."
    )


def render_subscription_truncated(reason: str, requested: int, subscribed: int, limit: int) -> str:
    """Subscription truncated below configured universe (P2 #8 fix).

    Args:
        reason: Truncation reason label (currently ``conn_limit``).
        requested: Number of symbols loaded into the client.
        subscribed: Number of symbols actually subscribed before truncation.
        limit: Per-conn cap (``MAX_SUBSCRIPTIONS_PER_CONN``) that triggered
            truncation.

    Returns:
        Formatted subscription-truncation notification string.
    """
    missed = max(0, requested - subscribed)
    return (
        "🔴 Quote subscription TRUNCATED\n"
        f"reason: {reason}\n"
        f"requested: {requested}\n"
        f"subscribed: {subscribed}\n"
        f"missed: {missed}\n"
        f"per_conn_limit: {limit}\n"
        "Symbols loaded but NEVER subscribed — increase HFT_QUOTE_CONNECTIONS "
        "or reduce config/symbols.yaml universe."
    )


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
    return f"🟡 券商重連 (第 {count} 次). Flap 狀態: {escape(flap_status)}."


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
        lines.append(f"Failed: {', '.join(escape(s) for s in failed_symbols[:5])}")
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
        f"PnL: {pnl_scaled // 10000} NTD | Strategies: {strategies_active} | Feed: {feed_status}"
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
        f"🟢 Backup {date_str} 完成\n大小: {size_mb:,.1f} MB | 耗時: {duration_s:.1f}s\n保留: {retained_count} 份備份"
    )


def render_margin_warning(ratio: float, used: int, available: int) -> str:
    """Margin utilization has reached the warning threshold.

    Args:
        ratio: Margin utilization ratio (0.0-1.0+).
        used: Margin used in NTD.
        available: Margin available in NTD.

    Returns:
        Formatted margin warning alert string.
    """
    return f"⚠️ 保證金警告\n使用率: {ratio:.1%}\n已用: {used:,} NTD\n可用: {available:,} NTD"


def render_margin_critical(ratio: float, used: int, available: int) -> str:
    """Margin utilization has reached critical threshold; reduce-only activated.

    Args:
        ratio: Margin utilization ratio (0.0-1.0+).
        used: Margin used in NTD.
        available: Margin available in NTD.

    Returns:
        Formatted margin critical alert string.
    """
    return f"🚨 保證金危急 — 已進入 reduce-only\n使用率: {ratio:.1%}\n已用: {used:,} NTD\n可用: {available:,} NTD"


def render_position_stuck(
    *,
    strategy_id: str,
    symbol: str,
    net_qty: int,
    age_s: int,
    unrealized_ntd: int | None = None,
) -> str:
    """Bug 27: position held too long without any new fills — possible deadlock.

    Args:
        strategy_id: Strategy that owns the stuck position.
        symbol: Instrument symbol.
        net_qty: Signed quantity (positive=long, negative=short).
        age_s: Seconds since last fill on this position.
        unrealized_ntd: Current unrealized PnL in NTD (optional).

    Returns:
        Formatted Telegram alert string.
    """
    side = "LONG" if net_qty > 0 else "SHORT"
    upnl_line = f"\n未實現損益: {unrealized_ntd:+d} NTD" if unrealized_ntd is not None else ""
    return (
        f"⚠️ 部位卡住\n"
        f"策略: {escape(strategy_id)}\n"
        f"合約: {escape(symbol)}  方向: {side}  口數: {abs(net_qty)}\n"
        f"持有時間: {age_s // 60} 分 {age_s % 60} 秒 (無新成交)"
        f"{upnl_line}\n"
        f"請檢查策略是否 deadlock。"
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
    return f"🔴 BACKUP 失敗 {date_str}\n錯誤: {escape(error)}\n最後成功備份: {last_success_date}\n請立即檢查備份磁碟"


def render_position_recovery(
    *,
    source: str,
    loaded: int,
    corrected: int,
    mismatches: list[dict],
) -> str:
    """Startup position recovery succeeded.

    Args:
        source: Recovery source (e.g. "dual", "checkpoint", "broker").
        loaded: Number of symbols loaded from source.
        corrected: Number of mismatches that were corrected.
        mismatches: List of correction details (symbols, actions).

    Returns:
        Formatted position recovery success notification string.
    """
    lines = [
        "🟢 部位恢復完成",
        f"來源: {source} | 載入: {loaded} symbols | 修正: {corrected}",
    ]
    for m in mismatches[:5]:
        symbol = m.get("symbol", "?")
        action = m.get("action", "?")
        lines.append(f"  {symbol}: {action}")
    return "\n".join(lines)


def render_tca_pnl_supplement(*, tca_section: str, pnl_section: str) -> str:
    """TCA + PnL supplement for the daily report.

    Args:
        tca_section: Formatted TCA (Transaction Cost Analysis) section text.
        pnl_section: Formatted PnL breakdown section text.

    Returns:
        Multi-line formatted TCA and PnL supplement string.
    """
    return f"📊 TCA & PnL Supplement\n\n{tca_section}\n\n{pnl_section}"


def render_position_recovery_failed(
    *,
    source: str,
    reason: str,
    mismatches: list[dict],
) -> str:
    """Startup position recovery failed — HALT triggered.

    Args:
        source: Recovery source (e.g. "dual", "checkpoint", "broker").
        reason: Human-readable reason for the failure.
        mismatches: List of mismatch details for diagnostics.

    Returns:
        Formatted position recovery failure alert string (HALT).
    """
    lines = [
        "🔴 部位恢復失敗 — HALT",
        f"來源: {source}",
        f"原因: {escape(reason)}",
    ]
    for m in mismatches[:5]:
        symbol = m.get("symbol", "?")
        ckpt_qty = m.get("checkpoint_qty", "?")
        broker_qty = m.get("broker_qty", "?")
        lines.append(f"  {symbol}: ckpt={ckpt_qty} broker={broker_qty}")
    lines.append("請手動確認部位後重啟")
    return "\n".join(lines)


def render_canary_action(alpha_id: str, action: str, reason: str) -> str:
    """Canary rollback or graduation notification.

    Args:
        alpha_id: Alpha identifier.
        action: Action taken ("rolled_back" or "graduated").
        reason: Human-readable reason.

    Returns:
        Formatted canary action alert string.
    """
    icon = "🔴" if action == "rolled_back" else "🟢"
    label = "ROLLBACK" if action == "rolled_back" else "GRADUATED"
    return f"{icon} Canary {label}: {escape(alpha_id)}\n{escape(reason)}"
