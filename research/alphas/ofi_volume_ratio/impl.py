"""OFI Volume Ratio Alpha — order flow imbalance normalized by volume.

Signal:  OVR_t = EMA_8((bid_qty - ask_qty) / max(volume, epsilon))
         positive → buy flow conviction, negative → sell flow conviction.

Hypothesis: Order flow imbalance normalized by volume separates informed from
noise flow. High |OFI/volume| indicates directional conviction; low ratio
indicates random noise.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="ofi_volume_ratio",
    hypothesis=(
        "Order flow imbalance normalized by volume separates informed from"
        " noise flow. High |OFI/volume| indicates directional conviction;"
        " low ratio indicates random noise."
    ),
    formula="OVR_t = EMA_8((bid_qty - ask_qty) / max(volume, epsilon))",
    paper_refs=(),
    data_fields=("bid_qty", "ask_qty", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class OfiVolumeRatioAlpha:
    """O(1) OFI-per-volume predictor with EMA smoothing.

    update() accepts either:
      - 3 positional args:  bid_qty, ask_qty, volume
      - keyword args:       bid_qty=..., ask_qty=..., volume=...
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
        """Compute one tick of OFI/volume ratio with EMA smoothing."""
        if len(args) >= 3:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
            volume = float(args[2])
        elif len(args) == 0:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
            volume = float(kwargs.get("volume", 0.0))
        else:
            raise ValueError(
                "update() requires 3 positional args (bid_qty, ask_qty, volume)"
                " or keyword args"
            )

        raw_ovr = (bid_qty - ask_qty) / max(volume, _EPSILON)

        if not self._initialized:
            self._ema = raw_ovr
            self._initialized = True
        else:
            self._ema += _EMA_ALPHA * (raw_ovr - self._ema)

        self._signal = self._ema
        return self._signal

    def reset(self) -> None:
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = OfiVolumeRatioAlpha

__all__ = ["OfiVolumeRatioAlpha", "ALPHA_CLASS"]
