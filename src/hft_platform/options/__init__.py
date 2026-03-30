"""Options analytics package — offline pricing, Greeks, vol surface.

Float exception: Per Architecture Governance Rule 25 §11, float is permitted
in this package for offline research computation. The live_adapter module
(Phase 2) is the boundary that converts float → int/bool before any value
enters the live trading path.
"""

from hft_platform.options.greeks import (
    AggregatedGreeks,
    GreeksResult,
    PositionGreeks,
    compute_greeks,
    portfolio_greeks,
)
from hft_platform.options.pricing import black76_price, solve_iv
from hft_platform.options.surface import VolSurface

__all__ = [
    "black76_price",
    "solve_iv",
    "compute_greeks",
    "portfolio_greeks",
    "GreeksResult",
    "PositionGreeks",
    "AggregatedGreeks",
    "VolSurface",
]
