"""C60 — TMFD6 R47-minimal maker under institutional RT.

Transfer of C33 PROMOTE mechanism (TXFD6 R47-minimal) to TMFD6 at inst-tier
RT 1.5 pt (shared-context.yaml#cost_model.TMF). Retail 4 pt cost drag 200%
drops to 75% drag under inst tier, making the mechanism math-viable.
"""

from __future__ import annotations

from .impl import (  # noqa: F401
    _DISABLED_SIGNAL_LAYERS_MOST,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    _TMF_RETAIL_RT_COST_PTS,
    C60Alpha,
    C60Params,
    TmfD6SoloMakerMinimal,
)

ALPHA_CLASS = C60Alpha

__all__ = [
    "ALPHA_CLASS",
    "C60Alpha",
    "C60Params",
    "TmfD6SoloMakerMinimal",
    "_DISABLED_SIGNAL_LAYERS_MOST",
    "_TMF_POINT_VALUE_NTD",
    "_TMF_INST_RT_COST_PTS",
    "_TMF_RETAIL_RT_COST_PTS",
]
