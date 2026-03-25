"""Daily PnL report: aggregation, Telegram formatting, and ClickHouse persistence."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from structlog import get_logger

logger = get_logger("ops.daily_pnl_report")


@dataclass
class DailyReportData:
    """Daily PnL report data. All monetary values in NTD (integer)."""

    report_date: str = ""
    strategy_id: str = ""
    symbol: str = ""
    realized_pnl_ntd: int = 0
    unrealized_pnl_ntd: int = 0
    fees_ntd: int = 0
    tax_ntd: int = 0
    orders_sent: int = 0
    orders_filled: int = 0
    orders_cancelled: int = 0
    avg_slippage_ticks: float = 0.0  # reporting metric, float OK per Architecture Rule 11
    slippage_cost_ntd: int = 0
    peak_pnl_ntd: int = 0
    max_drawdown_ntd: int = 0
    soft_limit_triggers: int = 0
    hard_limit_triggers: int = 0
    autonomy_transitions: int = 0
    win_count: int = 0
    loss_count: int = 0
    gross_profit_ntd: int = 0
    gross_loss_ntd: int = 0

    @property
    def net_pnl_ntd(self) -> int:
        return self.realized_pnl_ntd - self.fees_ntd - self.tax_ntd

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        if total == 0:
            return 0.0
        return self.win_count / total

    @property
    def profit_factor(self) -> float:
        if self.gross_loss_ntd == 0:
            return float("inf") if self.gross_profit_ntd > 0 else 0.0
        return self.gross_profit_ntd / self.gross_loss_ntd


class DailyPnLReport:
    """Generates and formats daily PnL reports."""

    @staticmethod
    def format_telegram(data: DailyReportData) -> str:
        """Format the daily summary Telegram message per the spec template."""
        lines = [
            f"📊 Daily Summary — {data.report_date}",
            "",
            f"Sessions: futures_day ✅",
            f"P&L: {data.realized_pnl_ntd:+d} NTD (realized) / {data.unrealized_pnl_ntd:+d} NTD (unrealized)",
            f"Orders: {data.orders_sent} sent / {data.orders_filled} filled / {data.orders_cancelled} cancelled",
            f"Fills: avg slippage {data.avg_slippage_ticks:.1f} 點/筆, cost {data.slippage_cost_ntd} NTD",
            f"Fees: {data.fees_ntd:+d} NTD (手續費) / {data.tax_ntd:+d} NTD (交易稅)",
            f"Net P&L: {data.net_pnl_ntd:+d} NTD",
            "",
            "Risk",
            f"  Peak PnL: {data.peak_pnl_ntd:+d} NTD / Max Drawdown: {data.max_drawdown_ntd:+d} NTD",
            f"  Soft Limit: {data.soft_limit_triggers} / Hard Limit: {data.hard_limit_triggers}",
            f"Autonomy: {data.autonomy_transitions} transitions",
        ]
        return "\n".join(lines)

    @staticmethod
    def to_clickhouse_row(data: DailyReportData) -> dict:
        """Convert to dict for ClickHouse insertion."""
        return {
            "report_date": data.report_date,
            "strategy_id": data.strategy_id,
            "symbol": data.symbol,
            "realized_pnl_ntd": data.realized_pnl_ntd,
            "unrealized_pnl_ntd": data.unrealized_pnl_ntd,
            "net_pnl_ntd": data.net_pnl_ntd,
            "fees_ntd": data.fees_ntd,
            "tax_ntd": data.tax_ntd,
            "orders_sent": data.orders_sent,
            "orders_filled": data.orders_filled,
            "orders_cancelled": data.orders_cancelled,
            "avg_slippage_ticks": data.avg_slippage_ticks,
            "slippage_cost_ntd": data.slippage_cost_ntd,
            "peak_pnl_ntd": data.peak_pnl_ntd,
            "max_drawdown_ntd": data.max_drawdown_ntd,
            "soft_limit_triggers": data.soft_limit_triggers,
            "hard_limit_triggers": data.hard_limit_triggers,
            "autonomy_transitions": data.autonomy_transitions,
            "win_count": data.win_count,
            "loss_count": data.loss_count,
            "profit_factor": data.profit_factor,
        }
