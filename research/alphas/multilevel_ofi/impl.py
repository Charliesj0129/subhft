"""Multi-Level Order-Flow Imbalance Alpha — ref 124.

Signal:  Σ_{k=1}^{5} w_k · (ΔBid_k - ΔAsk_k), smoothed via EMA_8.
Weights: w_k = exp(-0.5·(k-1)) → [1.0, 0.607, 0.368, 0.223, 0.135].

Extends L1 queue_imbalance (ref 125) to L1-L5 depth. Deeper price levels
add predictive power for mid-price changes (decaying exponentially).

Allocator Law  : __slots__ on class; prev-depth stored in pre-allocated numpy arrays.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Cache Law      : prev_{bid,ask}_qty are contiguous float64 arrays (5 elements each).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_N_LEVELS: int = 5
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)  # ≈ 0.1175

# Weights: exp(-0.5 * k) for k = 0..4
_WEIGHTS: np.ndarray = np.array([math.exp(-0.5 * k) for k in range(_N_LEVELS)], dtype=np.float64)
# Precomputed as tuple for documentation: (1.0, 0.6065, 0.3679, 0.2231, 0.1353)

_SIGNAL_CLIP: float = 2.0

# ---------------------------------------------------------------------------
# Manifest (Allocator Law: no per-call heap allocation)
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="multilevel_ofi",
    hypothesis=(
        "Multi-level order-flow imbalance across L1-L5 depth, weighted by "
        "exponentially decaying importance, predicts near-term mid-price "
        "direction more robustly than L1-only queue imbalance."
    ),
    formula="signal = EMA_8(Σ_{k=1}^{5} w_k · (ΔBid_k - ΔAsk_k)), w_k = exp(-0.5·(k-1))",
    paper_refs=("124",),
    data_fields=("bids", "asks"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class MultilevelOfiAlpha:
    """O(1) multi-level OFI predictor with EMA smoothing.

    Stores previous tick depth quantities (5 levels each side) as state.
    On each update, computes per-level delta, applies exponential weighting,
    sums, and smooths via EMA.

    update() accepts:
      - keyword args: bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
      - fallback:     bid_qty=float, ask_qty=float (L1 only)
      - positional:   bid_qty, ask_qty (L1 only)
    """

    __slots__ = (
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_cur_bid_qty",
        "_cur_ask_qty",
        "_ema",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        # Pre-allocated arrays for previous and current tick depth (Allocator Law)
        self._prev_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: object) -> float:  # noqa: C901
        """Update state with new depth data and return the current signal."""
        # --- resolve current depth quantities (reuse pre-allocated scratch) ---
        cur_bid = self._cur_bid_qty
        cur_ask = self._cur_ask_qty
        cur_bid[:] = 0.0
        cur_ask[:] = 0.0

        if "bids" in kwargs and "asks" in kwargs:
            bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
            asks = np.asarray(kwargs["asks"], dtype=np.float64).reshape(-1, 2)
            n_bid = min(bids.shape[0], _N_LEVELS)
            n_ask = min(asks.shape[0], _N_LEVELS)
            cur_bid[:n_bid] = bids[:n_bid, 1]
            cur_ask[:n_ask] = asks[:n_ask, 1]
        elif len(args) >= 2:
            cur_bid[0] = float(args[0])
            cur_ask[0] = float(args[1])
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            cur_bid[0] = float(kwargs["bid_qty"])  # type: ignore[arg-type]
            cur_ask[0] = float(kwargs["ask_qty"])  # type: ignore[arg-type]

        # --- compute weighted multi-level OFI ---
        delta_bid = cur_bid - self._prev_bid_qty
        delta_ask = cur_ask - self._prev_ask_qty
        raw_ofi = float(np.dot(_WEIGHTS, delta_bid - delta_ask))

        # --- store current as previous (in-place copy, no allocation) ---
        np.copyto(self._prev_bid_qty, cur_bid)
        np.copyto(self._prev_ask_qty, cur_ask)

        # --- EMA smoothing ---
        if not self._initialized:
            self._ema = raw_ofi
            self._initialized = True
        else:
            self._ema += _EMA_ALPHA * (raw_ofi - self._ema)

        # --- clip to [-2, 2] ---
        self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._ema))
        return self._signal

    def reset(self) -> None:
        """Clear all state."""
        self._prev_bid_qty[:] = 0.0
        self._prev_ask_qty[:] = 0.0
        self._cur_bid_qty[:] = 0.0
        self._cur_ask_qty[:] = 0.0
        self._ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = MultilevelOfiAlpha

__all__ = ["MultilevelOfiAlpha", "ALPHA_CLASS"]
