"""Edge-per-round-trip sub-gate (goal §5 hard bar).

Enforces the user-level promotion floor: a candidate strategy must
produce ``mean_net_edge_pts_per_trade > 10`` (configurable) measured on
**completed FIFO round-trip trades**, not on per-fill or per-quote
counts.

Edge is sourced from the maker/taker engine's per-day rollup:
``mean_net_edge_pts_per_trade = sum(daily_pnl[*].pnl_pts) /
sum(daily_pnl[*].trips)``.  ``pnl_pts`` is already cost-adjusted
(fees + tax via cost_model.apply) AND residual-MtM-folded (the engine
adds residual_mtm_pts into the gross before cost-model application —
see research/backtest/maker_engine.py:310-311).  Spread is captured in
the gross via FIFO fill-pair PnL; latency adverse selection and
force-flat costs are captured by the cost model + FORCE_FLAT
session-end policy (Slice B / Stage 8).

Therefore the numerator here satisfies goal §2 cost-deduction
requirements *transitively* — this gate's job is the per-trade arithmetic
and the >10-pt floor, not re-deducing costs.

Companion gates that must also be in the strict profile for this
floor to be trustworthy:
- ``inventory_mtm``       — proves residual was folded
- ``cost_uncertainty``    — proves cost model has tight CI
- ``min_sample_size``     — proves trip count is meaningful
- ``single_day_dominance``+ ``monthly_distribution`` — proves the edge
  isn't a single-day/-month artifact
"""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


def _int_field(entry: Any, key: str) -> int:
    if isinstance(entry, dict):
        return int(entry.get(key, 0) or 0)
    return 0


def _float_field(entry: Any, key: str, default: float = 0.0) -> float:
    if isinstance(entry, dict):
        return float(entry.get(key, default) or 0.0)
    if key == "pnl_pts":
        return float(entry)
    return default


class EdgePerRoundTripGate:
    """Reject runs whose per-completed-round-trip net edge fails the floor."""

    name = "edge_per_round_trip"
    applies_to = {"maker", "taker"}

    def evaluate(self, result: Any, config: Any, thresholds: dict) -> SubGateResult:
        daily = list(getattr(result, "daily_pnl", None) or [])
        floor = float(thresholds.get("mean_net_edge_pts_per_trade_min", 10.0))

        n_trips = sum(_int_field(e, "trips") for e in daily)
        n_fills = sum(_int_field(e, "fills") for e in daily)
        total_net = sum(_float_field(e, "pnl_pts") for e in daily)

        metrics_base = {
            "n_trips": float(n_trips),
            "n_fills": float(n_fills),
            "total_net_pts": float(total_net),
            "threshold_pts": float(floor),
        }

        if n_trips == 0 and n_fills == 0:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={**metrics_base, "mean_net_edge_pts_per_trade": 0.0},
                details="no activity (no fills, no trips) — gate skipped",
            )

        if n_trips == 0:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={**metrics_base, "mean_net_edge_pts_per_trade": 0.0},
                details=(
                    f"no completed round-trips ({n_fills} fills present) — "
                    "edge cannot be evaluated from one-sided exposure"
                ),
            )

        edge = total_net / n_trips
        passed = edge > floor  # strict > per goal §5 "edge > 10"
        metrics: dict[str, float | None] = {
            **metrics_base,
            "mean_net_edge_pts_per_trade": float(edge),
        }
        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics=metrics,
            details=(
                f"mean_net_edge={edge:.2f} pts/trade vs floor "
                f"{floor:.2f} (n_trips={n_trips}, total_net={total_net:.1f})"
            ),
        )
