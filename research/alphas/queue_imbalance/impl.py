"""Queue Imbalance Alpha — ref 125 (Gould & Bonart 2015).

Signal:  QI_t = (V_bid - V_ask) / (V_bid + V_ask)
Smoothed via exponential moving average (alpha_ema ≈ 1 - exp(-1/8)).

A positive QI → bid-side dominates → near-term upward mid-price pressure.
A negative QI → ask-side dominates → near-term downward pressure.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-01 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ≈ 8 ticks → α = 1 − exp(−1/8) ≈ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="queue_imbalance",
    hypothesis=(
        "Best-bid/best-ask queue size imbalance predicts the one-tick-ahead"
        " mid-price direction: a large bid queue signals upward price pressure,"
        " a large ask queue signals downward pressure."
    ),
    formula="QI_t = EMA_8( (V_bid - V_ask) / (V_bid + V_ask) )",
    paper_refs=("125",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.GATE_B,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-01",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class QueueImbalanceAlpha:
    """O(1) queue-imbalance predictor with EMA smoothing.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = ("_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args, **kwargs) -> float:
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

        denom = bid_qty + ask_qty
        raw_qi = (bid_qty - ask_qty) / (denom + _EPSILON)

        if not self._initialized:
            self._ema = raw_qi
            self._initialized = True
        else:
            self._ema += _EMA_ALPHA * (raw_qi - self._ema)

        self._signal = self._ema
        return self._signal

    def reset(self) -> None:
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = QueueImbalanceAlpha

__all__ = ["QueueImbalanceAlpha", "ALPHA_CLASS"]
