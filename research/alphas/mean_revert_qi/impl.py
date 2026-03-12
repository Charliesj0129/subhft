"""Mean-Revert Queue Imbalance Alpha — ref 098.

Hypothesis: Queue imbalance tends to mean-revert; extreme deviations from the
long-term mean predict contrarian price moves.

Formula:
  qi       = (bid - ask) / max(bid + ask, 1)          in [-1, 1]
  long_ema += alpha64 * (qi - long_ema)                EMA-64 of qi
  var_ema  += alpha32 * ((qi - long_ema)^2 - var_ema)  EMA-32 of squared deviation
  vol      = sqrt(var_ema)
  z        = (qi - long_ema) / max(vol, epsilon)
  signal   = -clip(z, -2, 2)                           contrarian: negate z

Signal range: [-2, 2].  Positive z (overbought) -> negative signal.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA coefficients
_A32: float = 1.0 - math.exp(-1.0 / 32.0)  # ~ 0.0308 — variance window
_A64: float = 1.0 - math.exp(-1.0 / 64.0)  # ~ 0.0155 — long-term mean window

_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="mean_revert_qi",
    hypothesis=(
        "Queue imbalance tends to mean-revert; extreme deviations from the "
        "long-term mean predict contrarian price moves."
    ),
    formula=("signal = -clip((qi - EMA64(qi)) / sqrt(EMA32((qi - EMA64(qi))^2)), -2, 2)"),
    paper_refs=("098",),
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


class MeanRevertQiAlpha:
    """O(1) mean-reversion z-score of queue imbalance (contrarian).

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = ("_long_ema", "_var_ema", "_signal")

    def __init__(self) -> None:
        self._long_ema: float = 0.0
        self._var_ema: float = 0.0
        self._signal: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state and return the current signal."""
        # --- resolve bid_qty / ask_qty from various call conventions ---
        if args and len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            bid_qty = float(kwargs["bid_qty"])
            ask_qty = float(kwargs["ask_qty"])
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(bids[0][1]) if hasattr(bids, "__getitem__") and len(bids) > 0 else 0.0
            ask_qty = float(asks[0][1]) if hasattr(asks, "__getitem__") and len(asks) > 0 else 0.0
        else:
            bid_qty = 0.0
            ask_qty = 0.0

        total = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(total, 1.0)  # in [-1, 1]

        # Long-term EMA of qi
        self._long_ema += _A64 * (qi - self._long_ema)

        # Variance EMA: track (qi - long_ema)^2
        deviation = qi - self._long_ema
        self._var_ema += _A32 * (deviation * deviation - self._var_ema)

        # Volatility and z-score
        vol = math.sqrt(max(self._var_ema, 0.0))
        z = deviation / max(vol, _EPSILON)

        # Contrarian: negate and clip to [-2, 2]
        self._signal = -max(-2.0, min(2.0, z))
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._long_ema = 0.0
        self._var_ema = 0.0
        self._signal = 0.0

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = MeanRevertQiAlpha

__all__ = ["MeanRevertQiAlpha", "ALPHA_CLASS"]
