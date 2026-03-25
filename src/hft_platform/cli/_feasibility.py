"""Feasibility validation scorecard — statistical pass/fail for strategy viability."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Sequence

from structlog import get_logger

logger = get_logger("cli.feasibility")


class Verdict(StrEnum):
    PASS = "PASS"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"


@dataclass
class FeasibilityScorecard:
    cumulative_net_pnl_ntd: int
    daily_pnl_values: Sequence[float]
    net_alpha_retention_rate: float
    hard_limit_triggers: int
    max_consecutive_loss_days: int
    p_threshold: float = 0.10

    @property
    def t_test_p_value(self) -> float:
        if len(self.daily_pnl_values) < 2:
            return 1.0
        from scipy.stats import ttest_1samp
        _, p = ttest_1samp(self.daily_pnl_values, 0)
        return float(p)

    @property
    def verdict(self) -> Verdict:
        if self.cumulative_net_pnl_ntd <= 0:
            return Verdict.FAIL
        if self.net_alpha_retention_rate < 0.50:
            return Verdict.FAIL
        if self.hard_limit_triggers > 1:
            return Verdict.FAIL
        if self.max_consecutive_loss_days > 3:
            return Verdict.FAIL
        if self.t_test_p_value > self.p_threshold:
            return Verdict.INCONCLUSIVE
        return Verdict.PASS


def cmd_feasibility_report(args: Any) -> None:
    """CLI entry point for `hft feasibility report`."""
    min_days = getattr(args, "min_days", 5)
    strategy = getattr(args, "strategy", "")
    logger.info("Feasibility report", min_days=min_days, strategy=strategy)
    print(f"Feasibility report: requires ClickHouse + {min_days}+ trading days of data.")
