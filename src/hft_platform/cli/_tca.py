"""Transaction Cost Analysis (TCA) attribution engine and CLI command."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Sequence

from structlog import get_logger

logger = get_logger("cli.tca")


@dataclass
class TradeAttribution:
    """PnL decomposition for a single round-trip trade."""
    fill_pnl_ntd: int      # actual realized PnL
    slippage_ntd: int       # cost from execution delay
    fees_ntd: int           # commissions + tax

    @property
    def gross_alpha_ntd(self) -> int:
        """What you'd have earned with perfect execution."""
        return self.fill_pnl_ntd + self.slippage_ntd

    @property
    def net_alpha_ntd(self) -> int:
        """True edge after all costs."""
        return self.gross_alpha_ntd - self.slippage_ntd - self.fees_ntd

    @property
    def retention_rate(self) -> float:
        """Fraction of gross alpha retained after costs."""
        gross = self.gross_alpha_ntd
        if gross <= 0:
            return 0.0
        return self.net_alpha_ntd / gross


class TCAEngine:
    """Offline TCA analysis engine."""

    @staticmethod
    def aggregate_by_dimension(
        records: Sequence[dict[str, Any]],
        group_key: str,
        value_key: str,
    ) -> dict[Any, float]:
        """Group records by dimension and compute mean of value."""
        groups: dict[Any, list[float]] = defaultdict(list)
        for r in records:
            groups[r[group_key]].append(float(r[value_key]))
        return {k: sum(v) / len(v) for k, v in groups.items()}


def cmd_tca_report(args: Any) -> None:
    """CLI entry point for `hft tca report`."""
    logger.info("TCA report", days=getattr(args, "days", 5), strategy=getattr(args, "strategy", ""))
    print("TCA report: requires ClickHouse connection. Use --days N to set lookback.")
