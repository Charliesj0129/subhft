"""Signed Volume EMA Alpha — volume-weighted directional flow.

Signal:  SVE_t = EMA_8( volume * (bid_qty - ask_qty) / (bid_qty + ask_qty) )

Hypothesis: Directional volume (volume signed by queue imbalance direction)
reveals net buying/selling pressure.  Sustained signed volume in one direction
predicts price continuation.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="signed_volume_ema",
    hypothesis=(
        "Directional volume (volume signed by queue imbalance direction) reveals"
        " net buying/selling pressure. Sustained signed volume in one direction"
        " predicts price continuation."
    ),
    formula="SVE_t = EMA_8(volume * (bid_qty - ask_qty) / (bid_qty + ask_qty))",
    paper_refs=(),
    data_fields=("volume", "bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class SignedVolumeEmaAlpha:
    """O(1) signed-volume-EMA predictor.

    update() accepts either:
      - 3 positional args:  volume, bid_qty, ask_qty
      - keyword args:       volume=..., bid_qty=..., ask_qty=...
    """

    __slots__ = ("_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update signal with new volume, bid_qty, ask_qty values."""
        # --- resolve volume, bid_qty, ask_qty ---
        if len(args) >= 3:
            volume = float(args[0])
            bid_qty = float(args[1])
            ask_qty = float(args[2])
        elif 0 < len(args) < 3:
            raise ValueError(
                "update() requires 3 positional args (volume, bid_qty, ask_qty)"
                " or keyword args"
            )
        else:
            volume = float(kwargs.get("volume", 0.0))
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        denom = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / (denom + _EPSILON)
        raw_sv = volume * imbalance

        if not self._initialized:
            self._ema = raw_sv
            self._initialized = True
        else:
            self._ema += _EMA_ALPHA * (raw_sv - self._ema)

        self._signal = self._ema
        return self._signal

    def reset(self) -> None:
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = SignedVolumeEmaAlpha

__all__ = ["SignedVolumeEmaAlpha", "ALPHA_CLASS"]
