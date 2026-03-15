"""Spread Mean-Reversion Alpha — spread deviation fade.

Signal:  SMR_t = -(spread_bps - EMA_32(spread_bps)) / EMA_32(spread_bps)
Hypothesis: Bid-ask spread mean-reverts after temporary widening. When spread
            exceeds its EMA, expect it to narrow — signal to provide liquidity.

Data fields: ("spread_bps",)

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constant: window ~ 32 ticks
_EMA_ALPHA_32: float = 1.0 - math.exp(-1.0 / 32.0)

_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="spread_mean_revert",
    hypothesis=(
        "Bid-ask spread mean-reverts after temporary widening. When spread"
        " exceeds its EMA, expect it to narrow — signal to provide liquidity."
    ),
    formula="SMR_t = -(spread_bps - EMA_32(spread_bps)) / EMA_32(spread_bps)",
    paper_refs=(),
    data_fields=("spread_bps",),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class SpreadMeanRevertAlpha:
    """O(1) spread mean-reversion signal with EMA_32 baseline.

    update() accepts either:
      - 1 positional arg:  spread_bps
      - keyword arg:       spread_bps=...
    """

    __slots__ = ("_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Ingest one tick of spread_bps and return the updated signal."""
        # --- resolve spread_bps from call conventions ---
        if len(args) >= 1:
            spread_bps = float(args[0])
        else:
            spread_bps = float(kwargs.get("spread_bps", 0.0))

        if not self._initialized:
            self._ema = spread_bps
            self._initialized = True
            # First tick: deviation is zero
            self._signal = 0.0
        else:
            self._ema += _EMA_ALPHA_32 * (spread_bps - self._ema)
            deviation = -(spread_bps - self._ema) / max(self._ema, _EPSILON)
            self._signal = deviation

        return self._signal

    def reset(self) -> None:
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = SpreadMeanRevertAlpha

__all__ = ["SpreadMeanRevertAlpha", "ALPHA_CLASS"]
