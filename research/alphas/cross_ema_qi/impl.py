"""Cross-EMA Queue Imbalance Alpha -- ref 127.

Signal:  qi = (bid - ask) / max(bid + ask, 1)
         fast = EMA_4(qi)
         slow = EMA_16(qi)
         signal = clip(fast - slow, -1, 1)

Hypothesis: EMA crossover (fast vs slow) of queue imbalance detects
momentum shifts earlier than single-EMA smoothing.

A positive crossover (fast > slow) signals building bid pressure.
A negative crossover (fast < slow) signals building ask pressure.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_A4: float = 1.0 - math.exp(-1.0 / 4.0)
_A16: float = 1.0 - math.exp(-1.0 / 16.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="cross_ema_qi",
    hypothesis=(
        "EMA crossover (fast vs slow) of queue imbalance detects momentum shifts earlier than single-EMA smoothing."
    ),
    formula=(
        "qi = (bid - ask) / max(bid + ask, 1); fast = EMA_4(qi); slow = EMA_16(qi); signal = clip(fast - slow, -1, 1)"
    ),
    paper_refs=("127",),
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


class CrossEmaQiAlpha:
    """O(1) cross-EMA queue-imbalance momentum detector.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = ("_fast_ema", "_slow_ema", "_signal")

    def __init__(self) -> None:
        self._fast_ema: float = 0.0
        self._slow_ema: float = 0.0
        self._signal: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args, **kwargs) -> float:
        # --- resolve bid_qty and ask_qty from various call conventions ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif len(args) == 1:
            raise ValueError("update() requires 2 positional args (bid_qty, ask_qty) or keyword args")
        elif "bids" in kwargs and "asks" in kwargs:
            import numpy as np  # lazy; not on hot path in research mode

            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(np.asarray(bids).reshape(-1, 2)[0, 1])
            ask_qty = float(np.asarray(asks).reshape(-1, 2)[0, 1])
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        denom = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(denom, 1.0)

        # Update fast and slow EMAs
        self._fast_ema += _A4 * (qi - self._fast_ema)
        self._slow_ema += _A16 * (qi - self._slow_ema)

        # Clipped crossover signal
        raw = self._fast_ema - self._slow_ema
        self._signal = max(-1.0, min(1.0, raw))
        return self._signal

    def reset(self) -> None:
        self._fast_ema = 0.0
        self._slow_ema = 0.0
        self._signal = 0.0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = CrossEmaQiAlpha

__all__ = ["CrossEmaQiAlpha", "ALPHA_CLASS"]
