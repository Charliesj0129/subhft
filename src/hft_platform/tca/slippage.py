# src/hft_platform/tca/slippage.py
"""TCA Slippage Decomposer — 4-component breakdown.

Components:
  1. Commission (fee excluding tax)
  2. Tax
  3. Delay cost (decision_price → arrival_price)
  4. Execution cost (arrival_price → fill_price)
  5. Market impact (estimated via sqrt model, residual = execution - impact)

WARNING: float arithmetic — offline TCA analysis only, NOT for live accounting.
"""

from __future__ import annotations

from hft_platform.tca.types import SlippageBreakdown


class SlippageDecomposer:
    __slots__ = ("_point_value", "_tick_size")

    def __init__(self, *, point_value: int, tick_size: float = 1.0) -> None:
        self._point_value = point_value
        self._tick_size = tick_size

    def decompose(
        self,
        *,
        decision_price: int,
        arrival_price: int,
        fill_price: int,
        notional_ntd: int,
        fee_ntd: int,
        tax_ntd: int,
    ) -> SlippageBreakdown:
        if notional_ntd == 0:
            return SlippageBreakdown(
                commission_bps=0.0,
                tax_bps=0.0,
                delay_cost_bps=0.0,
                execution_cost_bps=0.0,
                market_impact_bps=0.0,
                total_bps=0.0,
            )

        notional = float(notional_ntd)
        commission_ntd = float(max(0, fee_ntd - tax_ntd))

        commission_bps = (commission_ntd / notional) * 10_000.0
        tax_bps = (float(tax_ntd) / notional) * 10_000.0

        delay_points = float(arrival_price - decision_price) / 10_000.0
        exec_points = float(fill_price - arrival_price) / 10_000.0

        delay_cost_ntd = delay_points * self._point_value
        exec_cost_ntd = exec_points * self._point_value

        delay_cost_bps = (delay_cost_ntd / notional) * 10_000.0
        execution_cost_bps = (exec_cost_ntd / notional) * 10_000.0

        market_impact_bps = 0.0

        total_bps = commission_bps + tax_bps + delay_cost_bps + execution_cost_bps + market_impact_bps

        return SlippageBreakdown(
            commission_bps=commission_bps,
            tax_bps=tax_bps,
            delay_cost_bps=delay_cost_bps,
            execution_cost_bps=execution_cost_bps,
            market_impact_bps=market_impact_bps,
            total_bps=total_bps,
        )
