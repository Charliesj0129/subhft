"""Routes platform events to notification handlers."""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from hft_platform.notifications import templates

if TYPE_CHECKING:
    from hft_platform.notifications.telegram import TelegramSender

logger = structlog.get_logger(__name__)


class NotificationDispatcher:
    """Translates platform events into structured Telegram notifications.

    Each ``notify_*`` method maps to exactly one template render function and
    sets the ``critical`` flag appropriately:

    * **critical=True**: HALT and daily-loss-limit events (bypass rate limit).
    * **critical=False**: All other informational/warning events.
    """

    __slots__ = ("_sender",)

    def __init__(self, sender: TelegramSender) -> None:
        self._sender = sender

    # ------------------------------------------------------------------
    # Critical events (critical=True)
    # ------------------------------------------------------------------

    async def notify_halt(self, reason: str) -> None:
        """Notify operator that trading has been halted.

        Args:
            reason: Human-readable description of the halt cause.
        """
        msg = templates.render_halt(reason=reason)
        logger.warning("dispatcher.notify_halt", reason=reason)
        await self._sender.send(msg, critical=True)

    async def notify_daily_loss(self, pnl_ntd: int, limit_ntd: int) -> None:
        """Notify operator that the daily loss limit has been breached.

        Args:
            pnl_ntd: Current PnL in NTD (negative = loss).
            limit_ntd: Configured daily loss limit in NTD (negative value).
        """
        msg = templates.render_daily_loss(pnl_ntd=pnl_ntd, limit_ntd=limit_ntd)
        logger.warning("dispatcher.notify_daily_loss", pnl_ntd=pnl_ntd, limit_ntd=limit_ntd)
        await self._sender.send(msg, critical=True)

    # ------------------------------------------------------------------
    # Non-critical events (critical=False)
    # ------------------------------------------------------------------

    async def notify_stormguard_change(self, old: str, new: str, reason: str) -> None:
        """Notify operator of a StormGuard FSM state transition.

        Args:
            old: Previous StormGuard state name.
            new: New StormGuard state name.
            reason: Human-readable reason for the transition.
        """
        msg = templates.render_stormguard_change(old=old, new=new, reason=reason)
        logger.info("dispatcher.notify_stormguard_change", old=old, new=new, reason=reason)
        await self._sender.send(msg, critical=False)

    async def notify_pre_market_pass(self) -> None:
        """Notify operator that the pre-market health check passed."""
        msg = templates.render_pre_market_pass()
        logger.info("dispatcher.notify_pre_market_pass")
        await self._sender.send(msg, critical=False)

    async def notify_pre_market_fail(self, failed_checks: list[str]) -> None:
        """Notify operator that the pre-market health check failed.

        Args:
            failed_checks: List of check descriptions that failed.
        """
        msg = templates.render_pre_market_fail(failed_checks=failed_checks)
        logger.warning("dispatcher.notify_pre_market_fail", failed_checks=failed_checks)
        await self._sender.send(msg, critical=False)

    async def notify_reconciliation_mismatch(
        self,
        platform_pnl: int,
        broker_pnl: int,
        ch_pnl: int,
    ) -> None:
        """Notify operator of a PnL reconciliation mismatch.

        Args:
            platform_pnl: PnL as reported by platform position tracker (NTD).
            broker_pnl: PnL as reported by broker account gateway (NTD).
            ch_pnl: PnL as stored in ClickHouse fills (NTD).
        """
        msg = templates.render_reconciliation_mismatch(
            platform_pnl=platform_pnl,
            broker_pnl=broker_pnl,
            ch_pnl=ch_pnl,
        )
        logger.warning(
            "dispatcher.notify_reconciliation_mismatch",
            platform_pnl=platform_pnl,
            broker_pnl=broker_pnl,
            ch_pnl=ch_pnl,
        )
        await self._sender.send(msg, critical=False)

    async def notify_reconnect(self, count: int, flap_status: str) -> None:
        """Notify operator of a broker reconnect event.

        Args:
            count: Total reconnect count for the current session.
            flap_status: "OK", "FLAPPING", or similar descriptor from flap detector.
        """
        msg = templates.render_reconnect_alert(count=count, flap_status=flap_status)
        logger.info("dispatcher.notify_reconnect", count=count, flap_status=flap_status)
        await self._sender.send(msg, critical=False)

    async def notify_process_restart(self, attempt: int, max_attempts: int) -> None:
        """Notify operator that the supervisor is restarting a crashed subprocess.

        Args:
            attempt: Current restart attempt number (1-based).
            max_attempts: Maximum allowed restart attempts before giving up.
        """
        msg = templates.render_process_restart(attempt=attempt, max_attempts=max_attempts)
        logger.warning(
            "dispatcher.notify_process_restart",
            attempt=attempt,
            max_attempts=max_attempts,
        )
        await self._sender.send(msg, critical=False)

    async def notify_daily_report(
        self,
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
    ) -> None:
        """Send the end-of-day summary report.

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
        """
        msg = templates.render_daily_report(
            date_str=date_str,
            pnl_ntd=pnl_ntd,
            buys=buys,
            sells=sells,
            fills=fills,
            position_status=position_status,
            reconciliation_status=reconciliation_status,
            latency_p95_ms=latency_p95_ms,
            reconnect_count=reconnect_count,
            storm_guard_state=storm_guard_state,
            memory_gb=memory_gb,
            memory_max_gb=memory_max_gb,
        )
        logger.info("dispatcher.notify_daily_report", date_str=date_str, pnl_ntd=pnl_ntd)
        await self._sender.send(msg, critical=False)

    async def notify_weekly_summary(
        self,
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
    ) -> None:
        """Send the weekly performance summary.

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
        """
        msg = templates.render_weekly_summary(
            week_label=week_label,
            date_range=date_range,
            total_pnl_ntd=total_pnl_ntd,
            trading_days=trading_days,
            avg_trades=avg_trades,
            best_day_ntd=best_day_ntd,
            worst_day_ntd=worst_day_ntd,
            reconciliation_match=reconciliation_match,
            halt_count=halt_count,
            reconnect_count=reconnect_count,
            latency_p95_avg_ms=latency_p95_avg_ms,
            rss_peak_gb=rss_peak_gb,
            uptime_pct=uptime_pct,
        )
        logger.info(
            "dispatcher.notify_weekly_summary",
            week_label=week_label,
            total_pnl_ntd=total_pnl_ntd,
        )
        await self._sender.send(msg, critical=False)
