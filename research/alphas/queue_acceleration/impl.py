"""queue_acceleration — Queue Imbalance Acceleration Alpha.

Signal: second derivative (acceleration) of queue imbalance detects inflection
points where directional pressure is changing.

References:
  Paper 026: Unified Theory of Order Flow Impact

Formula:
  qi       = (bid - ask) / max(bid + ask, 1)
  velocity = EMA_8(qi) - EMA_32(qi)
  accel    = velocity - prev_velocity
  signal   = clip(EMA_4(accel), -1, 1)
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# EMA coefficients
# ---------------------------------------------------------------------------
_A4: float = 1.0 - math.exp(-1.0 / 4.0)  # ~ 0.2212 — accel smoothing
_A8: float = 1.0 - math.exp(-1.0 / 8.0)  # ~ 0.1175 — fast QI EMA
_A32: float = 1.0 - math.exp(-1.0 / 32.0)  # ~ 0.0308 — slow QI EMA

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="queue_acceleration",
    hypothesis=(
        "The acceleration (second derivative) of queue imbalance detects "
        "inflection points where directional pressure is changing."
    ),
    formula=(
        "qi = (bid - ask) / max(bid + ask, 1); "
        "velocity = EMA_8(qi) - EMA_32(qi); "
        "accel = velocity - prev_velocity; "
        "signal = clip(EMA_4(accel), -1, 1)"
    ),
    paper_refs=("026",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


# ---------------------------------------------------------------------------
# Alpha implementation
# ---------------------------------------------------------------------------
class QueueAccelerationAlpha:
    """O(1) queue-imbalance acceleration detector with EMA smoothing.

    State (all scalar, pre-allocated via __slots__):
      _ema8          : fast EMA of raw QI
      _ema32         : slow EMA of raw QI
      _prev_velocity : velocity from previous tick
      _accel_ema     : smoothed acceleration (EMA_4 of raw accel)
      _signal        : clipped output signal
      _initialized   : first-tick flag
    """

    __slots__ = ("_ema8", "_ema32", "_prev_velocity", "_accel_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema8: float = 0.0
        self._ema32: float = 0.0
        self._prev_velocity: float = 0.0
        self._accel_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state and return the current signal.

        Accepts positional ``(bid_qty, ask_qty)`` or keyword args.
        Also accepts ``bids`` / ``asks`` array-like for protocol compat.
        """
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        total = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(total, 1.0)

        if not self._initialized:
            self._ema8 = qi
            self._ema32 = qi
            self._prev_velocity = 0.0
            self._accel_ema = 0.0
            self._initialized = True
        else:
            self._ema8 += _A8 * (qi - self._ema8)
            self._ema32 += _A32 * (qi - self._ema32)

        velocity = self._ema8 - self._ema32
        accel = velocity - self._prev_velocity
        self._prev_velocity = velocity

        self._accel_ema += _A4 * (accel - self._accel_ema)

        self._signal = max(-1.0, min(1.0, self._accel_ema))
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._ema8 = 0.0
        self._ema32 = 0.0
        self._prev_velocity = 0.0
        self._accel_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = QueueAccelerationAlpha

__all__ = ["QueueAccelerationAlpha", "ALPHA_CLASS"]
