"""InventoryMtMGate — fail when net PnL after marking residual is below cost floor.

Slice B introduces residual MtM via Task 3's day-loop hook (see
``research/backtest/maker_engine.py``). This gate enforces that the realized PnL
plus the marked residual cannot meet the maker cost floor — i.e., the alpha's
edge cannot survive the un-FIFO'd inventory carrying cost.

Threshold: ``cost_floor_per_fill_pts`` (in
``config/alpha/promotion_profiles/vm_ul6_strict.yaml :: strategy_types.maker``).
When the threshold is absent, the gate returns advisory PASS (loose-profile
semantics).

Applies only to maker strategies (taker engines do not populate
``residual_mtm_pts``).
"""

from __future__ import annotations

from typing import Any

from hft_platform.alpha._sub_gates.registry import SubGateResult


class InventoryMtMGate:
    """Block promotion when realized + residual MtM falls below cost floor.

    Reads each row of ``result.daily_pnl`` for keys:
      * ``pnl_pts`` — realized PnL for the day in points.
      * ``fills`` — number of completed fills for the day.
      * ``residual_mtm_pts`` — mark-to-market value of un-FIFO'd inventory.

    Then compares ``sum(pnl_pts) + sum(residual_mtm_pts)`` to
    ``cost_floor_per_fill_pts * sum(fills)``.
    """

    name = "inventory_mtm"
    applies_to = {"maker"}

    def evaluate(
        self,
        result: Any,
        config: Any,
        thresholds: dict,
    ) -> SubGateResult:
        cost_floor = thresholds.get("cost_floor_per_fill_pts")
        is_strict = bool(thresholds.get("_is_strict_profile", False))
        daily = getattr(result, "daily_pnl", None) or []

        # Slice B requires dict rows with ``fills`` and ``residual_mtm_pts``.
        # Legacy float-only rows lack the bookkeeping needed for this gate.
        # Under loose profile: advisory PASS (back-compat with pre-Slice-B
        # scorecards).  Under strict profile: FAIL — strict promotion must
        # not silently accept a contract-shape mismatch as an OK result
        # (punch-list item 2, 2026-05-29).
        dict_rows = [row for row in daily if isinstance(row, dict)]
        if dict_rows:
            n_fills = sum(int(row.get("fills", 0) or 0) for row in dict_rows)
            realized = sum(float(row.get("pnl_pts", 0.0) or 0.0) for row in dict_rows)
            residual_mtm = sum(float(row.get("residual_mtm_pts", 0.0) or 0.0) for row in dict_rows)
            schema_advisory = False
        else:
            n_fills = 0
            realized = 0.0
            residual_mtm = 0.0
            schema_advisory = bool(daily)
        net_after_residual = realized + residual_mtm

        if schema_advisory:
            return SubGateResult(
                name=self.name,
                passed=not is_strict,
                metrics={
                    "realized_pts": 0.0,
                    "residual_mtm_pts": 0.0,
                    "net_pts": 0.0,
                    "cost_floor_per_fill_pts": (float(cost_floor) if cost_floor is not None else None),
                    "cost_floor_total_pts": None,
                    "n_fills": 0,
                },
                details=(
                    "STRICT FAIL: daily_pnl rows lack fills/residual_mtm_pts (legacy float-shape payload)"
                    if is_strict
                    else "advisory: daily_pnl rows lack fills/residual_mtm_pts (legacy float-shape payload)"
                ),
            )

        # Strict-only: empty daily_pnl is insufficient evidence.
        if is_strict and not dict_rows and not daily:
            return SubGateResult(
                name=self.name,
                passed=False,
                metrics={
                    "realized_pts": 0.0,
                    "residual_mtm_pts": 0.0,
                    "net_pts": 0.0,
                    "cost_floor_per_fill_pts": (float(cost_floor) if cost_floor is not None else None),
                    "cost_floor_total_pts": None,
                    "n_fills": 0,
                },
                details="STRICT FAIL: daily_pnl is empty (no evidence)",
            )

        if cost_floor is None:
            cost_floor_total: float | None = None
            passed = not is_strict
            details = (
                "STRICT FAIL: cost_floor_per_fill_pts threshold absent"
                if is_strict
                else "advisory: cost_floor_per_fill_pts threshold absent"
            )
        else:
            cost_floor_total = float(cost_floor) * n_fills
            passed = net_after_residual >= cost_floor_total
            details = (
                "OK" if passed else (f"net_pts={net_after_residual:.2f} below cost_floor_total={cost_floor_total:.2f}")
            )

        metrics: dict[str, Any] = {
            "realized_pts": round(realized, 4),
            "residual_mtm_pts": round(residual_mtm, 4),
            "net_pts": round(net_after_residual, 4),
            "cost_floor_per_fill_pts": (float(cost_floor) if cost_floor is not None else None),
            "cost_floor_total_pts": (round(cost_floor_total, 4) if cost_floor_total is not None else None),
            "n_fills": n_fills,
        }

        return SubGateResult(
            name=self.name,
            passed=passed,
            metrics=metrics,
            details=details,
        )
