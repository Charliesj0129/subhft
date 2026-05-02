"""C68 — TXF rollover-week back-to-front passive maker.

Mechanism per R4 T1 counterfactual (2026-04-19):
C68 quotes passively on the back-month TXF contract DURING the 3-day rollover
window when that contract is transitioning to become the new front-month.
Targets the narrow-spread 12-16 pt window observed in TXFD6's own 2026-02-23
to 2026-02-25 transition (the sole direct analog).

IMPORTANT departure from task-brief framing: Researcher T1 explicitly rejects
the "hedge pair" framing. Hedging on front TXFD6 via TAKE incurs 8.7 pt/RT
cost vs 7.8 pt/RT gross edge -> NEGATIVE NET. C68 is instead a SOLO PASSIVE
MAKER on the transitioning back-month, with calendar gating to activate only
during the narrow-spread rollover window. The "hedge" is a risk-offset
discipline (wait for opposite-side passive fill on same instrument), NOT a
cross-instrument taker leg.

Cost citation: shared-context.yaml#cost_model.TXF (inst RT 1.5 pt, confirmed=false)
Data caveat: 3-day analog window (TXFD6 Feb transition). T5 scorecard flags.
"""

from __future__ import annotations

from .impl import (  # noqa: F401
    _DISABLED_SIGNAL_LAYERS,
    _ROLLOVER_WINDOW_CANONICAL_DAYS,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    _TXF_RETAIL_RT_COST_PTS,
    C68Alpha,
    C68Params,
    TxfRolloverBackFrontMaker,
    is_in_rollover_window,
)

ALPHA_CLASS = C68Alpha

__all__ = [
    "ALPHA_CLASS",
    "C68Alpha",
    "C68Params",
    "TxfRolloverBackFrontMaker",
    "is_in_rollover_window",
    "_DISABLED_SIGNAL_LAYERS",
    "_TXF_POINT_VALUE_NTD",
    "_TXF_INST_RT_COST_PTS",
    "_TXF_RETAIL_RT_COST_PTS",
    "_ROLLOVER_WINDOW_CANONICAL_DAYS",
]
