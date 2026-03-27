"""Daily PnL report section — integrates into DailyReportService."""

from __future__ import annotations


class DailyPnlSection:
    """Formats daily PnL data into a Telegram-compatible HTML section."""

    __slots__ = ()

    def format_telegram_section(
        self,
        *,
        realized_pnl_ntd: int,
        unrealized_pnl_ntd: int,
        trade_count: int,
        fill_count: int,
    ) -> str:
        """Return a Telegram HTML-formatted PnL section.

        Args:
            realized_pnl_ntd: Realized PnL for the day in NTD.
            unrealized_pnl_ntd: Unrealized PnL (open positions) in NTD.
            trade_count: Number of trades placed.
            fill_count: Number of fills received.
        """
        total = realized_pnl_ntd + unrealized_pnl_ntd
        icon = "\U0001f4c8" if total >= 0 else "\U0001f4c9"
        return (
            f"<b>{icon} Daily PnL</b>\n"
            f"  Realized: {realized_pnl_ntd:,} NTD\n"
            f"  Unrealized: {unrealized_pnl_ntd:,} NTD\n"
            f"  Total: {total:,} NTD\n"
            f"  Trades: {trade_count} | Fills: {fill_count}"
        )
