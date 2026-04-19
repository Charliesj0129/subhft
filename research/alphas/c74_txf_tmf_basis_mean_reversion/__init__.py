"""C74 — TXF-TMF basis mean-reversion (cross-instrument directional pair trade).

Re-admit of R7 C66 (killed at 1:20 hedge-cost-dominant) under a DIFFERENT
mechanism: mean-reversion on the dollar-neutral basis (TXF - 20*TMF)
rather than passive two-side maker. Entry gate |basis - rolling_mean|
> 2 * rolling_stdev; exit on reversion or 30-min timeout; 4sigma TAKER
stop-loss.

Cost citation: shared-context.yaml#cost_model (TXF RT 1.5 + TMF RT 1.5 at
1:20 hedge = ~17.5 RT TMF equivalent; DA T2 confirmed cost_drag 20%).

Mutually exclusive with C63 on TXFD6 (inventory conflict per DA flag 9).
"""

from __future__ import annotations

from .impl import (  # noqa: F401
    _HEDGE_RATIO_TMF_PER_TXF,
    _STALE_QUOTE_FILTER_BASIS_PT,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    C74Alpha,
    C74Params,
    RollingBasisStats,
    TxfTmfBasisMeanReversion,
)

ALPHA_CLASS = C74Alpha

__all__ = [
    "ALPHA_CLASS",
    "C74Alpha",
    "C74Params",
    "TxfTmfBasisMeanReversion",
    "RollingBasisStats",
    "_HEDGE_RATIO_TMF_PER_TXF",
    "_STALE_QUOTE_FILTER_BASIS_PT",
    "_TXF_POINT_VALUE_NTD",
    "_TMF_POINT_VALUE_NTD",
    "_TXF_INST_RT_COST_PTS",
    "_TMF_INST_RT_COST_PTS",
]
