"""Trade-concentration sub-gate (goal 驗證標準 §5).

``single_day_dominance`` already guards against a single trading day
carrying the strategy's PnL.  Goal §5 also names trade-level
concentration: a strategy whose total PnL is one giant win has the
same problem as one whose PnL is one giant day — the rest of the
sample is noise around that one event, and the OOS verdict is
spurious.

This gate computes two trade-level pathology metrics:

  top_trade_share_pct = max(trade_pnl) / sum(trade_pnl)
      The single best winning trade as a fraction of total PnL.
      A single trade above the threshold fails the gate — every
      other trade combined cannot keep the candidate positive
      without that one outlier.

  worst_loss_share_pct = |min(trade_pnl)| / max(|sum(trade_pnl)|, 1)
      The worst single loss vs absolute total.  Captures the
      mirror pathology: a strategy whose downside is concentrated
      in one tail event will under-report drawdown risk on a small
      OOS sample.

Inputs: ``result.trade_pnl: list[float|dict]`` (canonical
round-trip PnL list, same shape ``outlier_trade_removal`` consumes).
If absent, falls back to ``result.daily_pnl`` (with the same
``_entry_to_float`` projection as the rest of the registry).

Thresholds:
  * top_trade_share_max_pct       — default 40.0 (any single
    winning trade above 40 % of total PnL fails)
  * worst_loss_share_max_pct      — default 50.0
"""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.common import _to_float_list
from hft_platform.alpha._sub_gates.registry import SubGateResult


class TradeConcentrationGate:
    """Reject candidates whose PnL is dominated by one trade."""

    name = "trade_concentration"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        top_max = float(thresholds.get("top_trade_share_max_pct", 40.0))
        worst_max = float(thresholds.get("worst_loss_share_max_pct", 50.0))

        trade_pnl: list[float] = _to_float_list(getattr(result, "trade_pnl", None))
        if not trade_pnl:
            trade_pnl = _to_float_list(getattr(result, "daily_pnl", None))

        if not trade_pnl:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={"n_trades": 0.0},
                details="no trade pnl — skip",
            )

        total = sum(trade_pnl)
        abs_total = abs(total) if total != 0 else 0.0
        biggest_win = max(trade_pnl)
        biggest_loss = min(trade_pnl)

        # Use abs(total) as denominator — relative share is the question;
        # division by zero collapses to a clamped 100 % so a zero-sum
        # strategy with non-trivial trades reads as 100 % concentrated.
        if abs_total == 0.0:
            top_share = 100.0 if biggest_win > 0 else 0.0
            worst_share = 100.0 if biggest_loss < 0 else 0.0
        else:
            top_share = 100.0 * biggest_win / abs_total if biggest_win > 0 else 0.0
            worst_share = (
                100.0 * abs(biggest_loss) / abs_total if biggest_loss < 0 else 0.0
            )

        top_passed = top_share <= top_max
        worst_passed = worst_share <= worst_max
        passed = top_passed and worst_passed

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "n_trades": float(len(trade_pnl)),
                "pnl_total": float(total),
                "biggest_win_pts": float(biggest_win),
                "biggest_loss_pts": float(biggest_loss),
                "top_trade_share_pct": float(top_share),
                "worst_loss_share_pct": float(worst_share),
                "top_trade_share_max_pct": top_max,
                "worst_loss_share_max_pct": worst_max,
            },
            details=(
                f"top {top_share:.1f}% (max {top_max:.1f}%), "
                f"worst-loss {worst_share:.1f}% (max {worst_max:.1f}%)"
            ),
        )
