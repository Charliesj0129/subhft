"""C63 — TXFD6 R47-minimal maker with tightened spread threshold under inst RT.

Variant of C33 (TXFD6 R47-minimal PROMOTE, R7-prior): lowers
`spread_threshold_pts` from 5 to 3 while keeping mp=3, queue_share=0.05,
R47-minimal (all signal layers DISABLED), non-|pos|-gated. Institutional-
tier RT 1.5 pt (shared-context.yaml#cost_model.TXF) halves retail RT 3 pt
and re-opens the 3 pt spread band.
"""

from __future__ import annotations

from .impl import (  # noqa: F401
    _DISABLED_SIGNAL_LAYERS,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    _TXF_RETAIL_RT_COST_PTS,
    C63Alpha,
    C63Params,
    TxfD6R47TightSpreadMaker,
)

ALPHA_CLASS = C63Alpha

__all__ = [
    "ALPHA_CLASS",
    "C63Alpha",
    "C63Params",
    "TxfD6R47TightSpreadMaker",
    "_DISABLED_SIGNAL_LAYERS",
    "_TXF_POINT_VALUE_NTD",
    "_TXF_INST_RT_COST_PTS",
    "_TXF_RETAIL_RT_COST_PTS",
]
