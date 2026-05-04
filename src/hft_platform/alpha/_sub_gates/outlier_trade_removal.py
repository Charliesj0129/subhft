"""Outlier-trade removal sub-gate."""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._resampling import drop_top_trades
from hft_platform.alpha._sub_gates.registry import SubGateResult


def _entry_to_float(entry: Any) -> float:
    if isinstance(entry, dict):
        return float(entry.get("pnl_pts", 0.0))
    return float(entry)


class OutlierTradeRemovalGate:
    """Sign of total PnL must survive removing the top X% of trades."""

    name = "outlier_trade_removal"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        pct_value = float(thresholds.get("outlier_trade_removal_pct", 0.0))
        pct = pct_value / 100.0 if pct_value > 1.0 else pct_value

        trade_pnl = list(getattr(result, "trade_pnl", None) or [])
        if not trade_pnl:
            daily = [_entry_to_float(e) for e in (getattr(result, "daily_pnl", None) or [])]
            if not daily:
                return SubGateResult(
                    name=self.name,
                    passed=False,
                    metrics={},
                    details="no trade or daily pnl to evaluate",
                )
            trade_pnl = daily

        total = sum(trade_pnl)
        kept = drop_top_trades(trade_pnl, pct=pct)
        residual = sum(kept)
        target_sign = 1 if total > 0 else (-1 if total < 0 else 0)
        residual_sign = 1 if residual > 0 else (-1 if residual < 0 else 0)
        passed = (target_sign == residual_sign) and target_sign != 0 if pct > 0 else True

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "drop_pct": float(pct * 100.0),
                "n_trades_in": float(len(trade_pnl)),
                "n_trades_kept": float(len(kept)),
                "pnl_total": float(total),
                "pnl_after_drop": float(residual),
            },
            details=(f"drop top {pct * 100:.1f}%: {total:.2f} -> {residual:.2f}"),
        )
