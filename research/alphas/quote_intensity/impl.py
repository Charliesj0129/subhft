"""Quote Intensity Alpha — directional quote activity signal.

Signal:  QI_t = EMA_8(|Δbid_qty| + |Δask_qty|) × sign(bid_qty - ask_qty)
                / EMA_32(|Δbid_qty| + |Δask_qty|)

Normalized directional quote activity: positive = active bid-side quoting
(informed buying pressure), negative = active ask-side quoting (selling
pressure).  The EMA_8/EMA_32 ratio captures intensity bursts relative to
the longer-term baseline.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants.
# Window ≈ 8 ticks  → α = 1 − exp(−1/8)  ≈ 0.1175
# Window ≈ 32 ticks → α = 1 − exp(−1/32) ≈ 0.0308
_EMA_ALPHA_FAST: float = 1.0 - math.exp(-1.0 / 8.0)
_EMA_ALPHA_SLOW: float = 1.0 - math.exp(-1.0 / 32.0)
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="quote_intensity",
    hypothesis=(
        "The rate of quote (bid/ask) changes signals information arrival"
        " intensity. Rapid quote updates indicate active repositioning by"
        " informed market makers. Quote intensity multiplied by imbalance"
        " direction predicts next-tick moves."
    ),
    formula=(
        "QI_t = EMA_8(|Δbid_qty| + |Δask_qty|) × sign(bid_qty - ask_qty)"
        " / EMA_32(|Δbid_qty| + |Δask_qty|)"
    ),
    paper_refs=(),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class QuoteIntensityAlpha:
    """O(1) directional quote-intensity predictor with dual-EMA normalization.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = (
        "_ema_fast",
        "_ema_slow",
        "_signal",
        "_initialized",
        "_prev_bid_qty",
        "_prev_ask_qty",
    )

    def __init__(self) -> None:
        self._ema_fast: float = 0.0
        self._ema_slow: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._prev_bid_qty: float = 0.0
        self._prev_ask_qty: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        # --- resolve bid_qty and ask_qty from various call conventions ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (bid_qty, ask_qty) or keyword args"
            )
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np  # lazy; not on hot path in research mode

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        # Compute absolute deltas from previous tick.
        delta_bid = abs(bid_qty - self._prev_bid_qty)
        delta_ask = abs(ask_qty - self._prev_ask_qty)
        raw_activity = delta_bid + delta_ask

        # Directional sign from current queue imbalance.
        imbalance = bid_qty - ask_qty
        if imbalance > 0:
            direction = 1.0
        elif imbalance < 0:
            direction = -1.0
        else:
            direction = 0.0

        # Update EMAs.
        if not self._initialized:
            self._ema_fast = raw_activity
            self._ema_slow = raw_activity
            self._initialized = True
        else:
            self._ema_fast += _EMA_ALPHA_FAST * (raw_activity - self._ema_fast)
            self._ema_slow += _EMA_ALPHA_SLOW * (raw_activity - self._ema_slow)

        # Normalized directional intensity.
        ratio = self._ema_fast / (self._ema_slow + _EPSILON)
        self._signal = ratio * direction

        # Store current quantities for next delta computation.
        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

        return self._signal

    def reset(self) -> None:
        self._ema_fast = 0.0
        self._ema_slow = 0.0
        self._signal = 0.0
        self._initialized = False
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = QuoteIntensityAlpha

__all__ = ["QuoteIntensityAlpha", "ALPHA_CLASS"]
