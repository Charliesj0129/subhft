"""c75_tmf_mw_ofi_taker -- Multi-Window OFI Taker (FE-v3, frozen weights).

This package conforms to ``research.registry.schemas.AlphaProtocol`` via
``ALPHA_CLASS = C75TmfMwOfiTakerAlpha``.  See ``impl.py`` for the strategy
formula and ``manifest.yaml`` for governance metadata.
"""

from research.alphas.c75_tmf_mw_ofi_taker.impl import ALPHA_CLASS, C75TmfMwOfiTakerAlpha

__all__ = ["ALPHA_CLASS", "C75TmfMwOfiTakerAlpha"]
