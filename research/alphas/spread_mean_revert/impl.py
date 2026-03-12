"""Spread Mean-Reversion Alpha — spread deviation fade.

Signal:  -EMA_8((spread_scaled - EMA_64(spread_scaled)) / max(EMA_64(spread_scaled), 1))
Hypothesis: Spread deviations from their long-term baseline are mean-reverting.
            Wide spread -> expect contraction. Negative sign = fade the deviation.

Data fields: ("spread_scaled",)

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)   # window ~ 8 ticks
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)  # window ~ 64 ticks

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="spread_mean_revert",
    hypothesis=(
        "Spread deviations from their long-term EMA baseline are mean-reverting."
        " A wide spread signals upcoming contraction; a narrow spread signals"
        " upcoming widening. The negative sign fades the deviation."
    ),
    formula="-EMA_8((spread_scaled - EMA_64(spread_scaled)) / max(EMA_64(spread_scaled), 1))",
    paper_refs=(),
    data_fields=("spread_scaled",),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class SpreadMeanRevertAlpha:
    """O(1) spread mean-reversion signal with dual-EMA smoothing.

    update() accepts either:
      - 1 positional arg:  spread_scaled
      - keyword arg:       spread_scaled=...
    """

    __slots__ = ("_spread_ema64", "_dev_ema8", "_signal", "_initialized")

    def __init__(self) -> None:
        self._spread_ema64: float = 0.0
        self._dev_ema8: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: object, **kwargs: object) -> float:
        """Ingest one tick of spread_scaled and return the updated signal."""
        # --- resolve spread_scaled from call conventions ---
        if len(args) >= 1:
            spread_scaled = float(args[0])  # type: ignore[arg-type]
        else:
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))  # type: ignore[arg-type]

        if not self._initialized:
            self._spread_ema64 = spread_scaled
            self._dev_ema8 = 0.0
            self._initialized = True
        else:
            self._spread_ema64 += _EMA_ALPHA_64 * (spread_scaled - self._spread_ema64)
            deviation = (spread_scaled - self._spread_ema64) / max(
                self._spread_ema64, 1.0
            )
            self._dev_ema8 += _EMA_ALPHA_8 * (deviation - self._dev_ema8)

        self._signal = -self._dev_ema8
        return self._signal

    def reset(self) -> None:
        self._spread_ema64 = 0.0
        self._dev_ema8 = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = SpreadMeanRevertAlpha

__all__ = ["SpreadMeanRevertAlpha", "ALPHA_CLASS"]
