"""Force-flat residual dominance sub-gate (goal 驗證標準 §3 + §5).

Round 41 added force-flat handling so the per-trip projector closes
any end-of-run inventory at the final mid (so residual losses can't
hide).  Round 42 surfaced ``abs_residual_qty`` + ``mark_method`` on
the ``BacktestResult`` for the taker side.  This gate consumes those
fields to flag candidates whose ``mean_net_edge_pts_per_trade`` is
propped up by force-flat trips — those edges aren't strategy alpha,
they're an artifact of how end-of-window inventory was marked.

Inputs:
  * ``result.abs_residual_qty`` (int)  — count of unit positions
    that were force-flat closed at end of run.
  * ``result.mark_method`` (str)       — informational; reported
    in details when ``force_flat_last_mid``.
  * ``result.trade_pnl`` (list[float]) — used to derive n_trips
    (denominator).  Empty → gate is advisory (no trips to share).

Threshold:
  ``force_flat_trip_share_max_pct`` — when the upper-bound force-flat
  trip share exceeds this, FAIL.  Default 30.0 (strict): the
  candidate's edge cannot be dominated by inventory carried into the
  forced close.

Upper-bound estimator: ``min(abs_residual_qty, n_trips) / n_trips``.
``abs_residual_qty`` counts synthetic-unit force-flat closes; under
FIFO each such close can match at most one earlier-opened unit, so
this is the tightest bound the result-side fields support without
re-running the projector.
"""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.common import _to_float_list
from hft_platform.alpha._sub_gates.registry import SubGateResult


class ForceFlatResidualGate:
    """Reject when force-flat residual dominates the round-trip list."""

    name = "force_flat_residual"
    applies_to = {"maker", "taker"}

    def evaluate(
        self,
        result: Any,
        config: Any,
        thresholds: dict,
    ) -> SubGateResult:
        max_share = thresholds.get("force_flat_trip_share_max_pct")
        abs_residual = int(getattr(result, "abs_residual_qty", 0) or 0)
        mark_method = str(getattr(result, "mark_method", "") or "")
        trade_pnl = _to_float_list(getattr(result, "trade_pnl", None))
        n_trips = len(trade_pnl)

        # No trips → no denominator → advisory PASS (the candidate has
        # no matched edge to defend; let upstream sample-size gates
        # handle the empty case).
        if n_trips == 0:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={
                    "abs_residual_qty": float(abs_residual),
                    "n_trips": 0.0,
                    "force_flat_trip_share_pct": 0.0,
                    "force_flat_trip_share_max_pct": (float(max_share) if max_share is not None else None),
                },
                details="no trade pnl — advisory skip",
            )

        # No force-flat residual → automatic PASS regardless of threshold.
        if abs_residual == 0:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={
                    "abs_residual_qty": 0.0,
                    "n_trips": float(n_trips),
                    "force_flat_trip_share_pct": 0.0,
                    "force_flat_trip_share_max_pct": (float(max_share) if max_share is not None else None),
                },
                details=f"no residual ({mark_method or 'no_residual'})",
            )

        ff_trips_upper = min(abs_residual, n_trips)
        ff_share_pct = 100.0 * ff_trips_upper / n_trips

        # Threshold absent → advisory metrics only, PASS by default
        # (loose-profile semantics consistent with other sub-gates).
        if max_share is None:
            return SubGateResult(
                name=self.name,
                passed=True,
                metrics={
                    "abs_residual_qty": float(abs_residual),
                    "n_trips": float(n_trips),
                    "force_flat_trip_share_pct": float(ff_share_pct),
                    "force_flat_trip_share_max_pct": None,
                },
                details=(f"advisory: ff_share={ff_share_pct:.1f}% (threshold absent; mark={mark_method or 'unknown'})"),
            )

        max_share_f = float(max_share)
        passed = ff_share_pct <= max_share_f
        details = (
            f"ff_share={ff_share_pct:.1f}% (max {max_share_f:.1f}%) "
            f"residual_qty={abs_residual} n_trips={n_trips} mark={mark_method or 'unknown'}"
        )

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics={
                "abs_residual_qty": float(abs_residual),
                "n_trips": float(n_trips),
                "force_flat_trip_share_pct": float(ff_share_pct),
                "force_flat_trip_share_max_pct": max_share_f,
            },
            details=details,
        )
