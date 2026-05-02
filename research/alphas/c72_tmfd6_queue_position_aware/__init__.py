"""C72 — TMFD6 Queue-Position-Aware Maker.

Overlay on C60 (PROMOTED TMFD6 R47-minimal): quote only when near-side L1
queue is thin (bid_qty <= threshold for buy-side; ask_qty <= threshold for
sell-side). CK-observable proxy for "self-queue-position near top" per
Researcher T1 (real queue rank is simulation-internal; using L1 depth proxy
avoids re-introducing PowerProb 14x pessimism — R47 SKILL lesson).

Non-|pos|-gated — gate is on OBSERVABLE L1 QUEUE DEPTH, not |pos|. Avoids
C22-class meta-kill.

Cost citation: shared-context.yaml#cost_model.TMF (inst RT 1.5pt, confirmed=false)
"""

from __future__ import annotations

from .impl import (  # noqa: F401
    _DISABLED_SIGNAL_LAYERS_MOST,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    _TMF_RETAIL_RT_COST_PTS,
    C72Alpha,
    C72Params,
    TmfD6QueuePositionAwareMaker,
)

ALPHA_CLASS = C72Alpha

__all__ = [
    "ALPHA_CLASS",
    "C72Alpha",
    "C72Params",
    "TmfD6QueuePositionAwareMaker",
    "_DISABLED_SIGNAL_LAYERS_MOST",
    "_TMF_POINT_VALUE_NTD",
    "_TMF_INST_RT_COST_PTS",
    "_TMF_RETAIL_RT_COST_PTS",
]
