"""Slippage decomposition — 4-component cost breakdown per fill."""
from __future__ import annotations

from hft_platform.contracts.strategy import Side
from hft_platform.tca.types import SlippageBreakdown


class SlippageDecomposer:
    """Decompose per-fill cost into commission, tax, delay, and execution components.

    Direction convention: positive = adverse, negative = favorable.
    All prices are expected as scaled integers (x10000). All bps results are float
    and are restricted to offline TCA analysis only (see SlippageBreakdown docstring).
    """

    __slots__ = ()

    def decompose(self, fill, notional_ntd: float, market_impact_bps: float = 0.0) -> SlippageBreakdown:
        """Decompose a fill into its cost components.

        Args:
            fill: A fill-like object with attributes: fee (int, scaled x10000),
                  tax (int, scaled x10000), decision_price (int, scaled x10000),
                  arrival_price (int, scaled x10000), price (int, scaled x10000),
                  side (Side).
            notional_ntd: Trade notional in NTD (unscaled).
            market_impact_bps: External market impact estimate in bps.

        Returns:
            SlippageBreakdown with all components in bps.
        """
        if notional_ntd > 0:
            comm_bps = (fill.fee / 10_000) / notional_ntd * 10_000
            tax_bps = (fill.tax / 10_000) / notional_ntd * 10_000
        else:
            comm_bps = 0.0
            tax_bps = 0.0

        if fill.decision_price > 0 and fill.arrival_price > 0:
            if fill.side == Side.BUY:
                delay = (fill.arrival_price - fill.decision_price) / fill.decision_price * 10_000
            else:
                delay = (fill.decision_price - fill.arrival_price) / fill.decision_price * 10_000
        else:
            delay = 0.0

        if fill.arrival_price > 0:
            if fill.side == Side.BUY:
                exec_cost = (fill.price - fill.arrival_price) / fill.arrival_price * 10_000
            else:
                exec_cost = (fill.arrival_price - fill.price) / fill.arrival_price * 10_000
        else:
            exec_cost = 0.0

        total = comm_bps + tax_bps + delay + exec_cost + market_impact_bps
        return SlippageBreakdown(
            commission_bps=comm_bps,
            tax_bps=tax_bps,
            delay_cost_bps=delay,
            execution_cost_bps=exec_cost,
            market_impact_bps=market_impact_bps,
            total_bps=total,
        )
