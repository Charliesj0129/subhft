"""TCA Report Generator — formats daily/weekly TCA summaries.

Designed to integrate as a section within DailyReportService, not standalone.
"""

from __future__ import annotations

from hft_platform.tca.types import TCADailyReport


class TCAReportGenerator:
    """Formats TCADailyReport objects into Telegram-compatible HTML sections."""

    __slots__ = ()

    def format_telegram_section(self, reports: list[TCADailyReport]) -> str:
        """Return a Telegram HTML-formatted TCA section, or empty string if no reports."""
        if not reports:
            return ""

        lines: list[str] = ["<b>TCA Summary</b>"]
        for r in reports:
            lines.append(
                f"\n<b>{r.strategy} / {r.symbol}</b>\n"
                f"  Trades: {r.trade_count} | Vol: {r.volume}\n"
                f"  Cost: {r.total_cost_bps_mean:.1f} bps (P95: {r.total_cost_bps_p95:.1f})\n"
                f"  Comm: {r.commission_bps_mean:.1f} | Tax: {r.tax_bps_mean:.1f} | "
                f"Delay: {r.delay_cost_bps_mean:.1f} | Exec: {r.exec_cost_bps_mean:.1f}"
            )
        return "\n".join(lines)
