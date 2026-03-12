"""OFI Acceleration Alpha — second derivative of order flow imbalance.

Signal:  EMA_8(ofi_l1_raw_t - ofi_l1_raw_{t-1})
Hypothesis: Second derivative of order flow (acceleration) predicts momentum
shifts. Positive acceleration = increasing buy pressure = upward.
Deceleration = potential reversal.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8  # guards against numerical edge cases

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="ofi_acceleration",
    hypothesis=(
        "Second derivative of order flow (acceleration) predicts momentum"
        " shifts: positive acceleration signals increasing buy pressure"
        " (upward), deceleration signals potential reversal."
    ),
    formula="EMA_8(ofi_l1_raw_t - ofi_l1_raw_{t-1})",
    paper_refs=(),
    data_fields=("ofi_l1_raw",),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class OfiAccelerationAlpha:
    """O(1) OFI acceleration predictor with EMA smoothing.

    update() accepts either:
      - 1 positional arg:  ofi_l1_raw
      - keyword arg:       ofi_l1_raw=...
    """

    __slots__ = ("_prev_ofi", "_accel_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_ofi: float = 0.0
        self._accel_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        # --- resolve ofi_l1_raw from call conventions ---
        if len(args) >= 1:
            ofi_l1_raw = float(args[0])
        else:
            ofi_l1_raw = float(kwargs.get("ofi_l1_raw", 0.0))

        if not self._initialized:
            # First tick: store prev, return 0 (no delta available yet).
            self._prev_ofi = ofi_l1_raw
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute delta (acceleration = first difference of OFI).
        delta = ofi_l1_raw - self._prev_ofi
        self._prev_ofi = ofi_l1_raw

        # EMA update.
        self._accel_ema += _EMA_ALPHA_8 * (delta - self._accel_ema)
        self._signal = self._accel_ema
        return self._signal

    def reset(self) -> None:
        self._prev_ofi = 0.0
        self._accel_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = OfiAccelerationAlpha

__all__ = ["OfiAccelerationAlpha", "ALPHA_CLASS"]
