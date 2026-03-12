"""Cumulative OFI Reversion Alpha — mean-reversion on cumulative order flow.

Signal:  -EMA_16( ofi_l1_cum / max(EMA_64(|ofi_l1_cum|), 1) )

Hypothesis: Cumulative OFI is mean-reverting on longer horizons.  A large
positive cumOFI indicates the price is overextended upward; fading it (negative
sign) bets on reversion.  A large negative cumOFI indicates downward
overextension; the positive resulting signal bets on upward reversion.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants.
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="cum_ofi_revert",
    hypothesis=(
        "Cumulative OFI is mean-reverting on longer horizons: large positive"
        " cumOFI signals upward overextension (fade with short), large negative"
        " cumOFI signals downward overextension (fade with long)."
    ),
    formula="-EMA_16( ofi_l1_cum / max(EMA_64(|ofi_l1_cum|), 1) )",
    paper_refs=(),
    data_fields=("ofi_l1_cum",),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class CumOfiRevertAlpha:
    """O(1) cumulative-OFI mean-reversion signal with dual-EMA smoothing.

    update() accepts either:
      - 1 positional arg:  ofi_l1_cum
      - keyword arg:       ofi_l1_cum=...
    """

    __slots__ = ("_abs_cum_ema64", "_norm_ema16", "_signal", "_initialized")

    def __init__(self) -> None:
        self._abs_cum_ema64: float = 0.0
        self._norm_ema16: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one step of the cumulative-OFI reversion signal."""
        if len(args) >= 1:
            ofi_l1_cum = float(args[0])
        else:
            ofi_l1_cum = float(kwargs.get("ofi_l1_cum", 0.0))

        abs_cum = abs(ofi_l1_cum)

        if not self._initialized:
            self._abs_cum_ema64 = abs_cum
            normalized = ofi_l1_cum / max(self._abs_cum_ema64, 1.0)
            self._norm_ema16 = normalized
            self._initialized = True
        else:
            self._abs_cum_ema64 += _EMA_ALPHA_64 * (abs_cum - self._abs_cum_ema64)
            normalized = ofi_l1_cum / max(self._abs_cum_ema64, 1.0)
            self._norm_ema16 += _EMA_ALPHA_16 * (normalized - self._norm_ema16)

        self._signal = -self._norm_ema16
        return self._signal

    def reset(self) -> None:
        self._abs_cum_ema64 = 0.0
        self._norm_ema16 = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = CumOfiRevertAlpha

__all__ = ["CumOfiRevertAlpha", "ALPHA_CLASS"]
