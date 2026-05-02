"""Multi-Level Depth Momentum (MLDM) — L2-L5 depth withdrawal detector.

Signal: EMA-smoothed net depth change at L2-L5 levels, orthogonal to L1 OFI.

Theory:
    Informed traders withdraw deep-book liquidity (cancel limit orders at L2-L5)
    before aggressively trading at L1.  This "depth withdrawal" precedes price
    moves by 500ms-5s but is invisible to L1 OFI, which only tracks best bid/ask.

    MLDM = EMA_fast(sum(delta_bid[2:5]) - sum(delta_ask[2:5]))
           - EMA_slow(sum(delta_bid[2:5]) - sum(delta_ask[2:5]))

BBO-shift guard (Execution review):
    When best bid or ask price changes, L2-L5 queue indices shift (what was L2
    becomes L3, etc.).  This creates spurious large deltas.  Guard: when best
    price changes, MLDM input for that tick is zeroed to avoid false signals.

Thin LOB guard (Execution review):
    When fewer than 2 levels available, deep_net is zeroed.

Differentiation from ofi_depth_divergence:
    ofi_depth_divergence measures the *relative momentum* between shallow (L1-L2)
    and deep (L3-L5) bands — it detects which band LEADS.
    MLDM measures the *absolute depth momentum* at L2-L5 only — it detects
    whether deep liquidity is growing or shrinking, independent of L1 activity.

Paper refs:
  Arroyo et al. (2023) arXiv:2306.05479 — fill probability & queue depletion
  Albers et al. (2025) arXiv:2502.18625 — queue position vs adverse selection
  Cont, Cucuringu, Zhang (2023) arXiv:2112.13213 — multi-level OFI

Allocator Law  : __slots__; pre-allocated numpy arrays for depth tracking.
Precision Law  : output is float signal score (not price).
Cache Law      : numpy arrays for contiguous depth state; scalar EMA accumulators.
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_N_LEVELS: int = 5
_MIN_LEVELS: int = 2
_L2_START: int = 1
_L2_END: int = 5
_EMA_FAST: float = 1.0 - math.exp(-1.0 / 8.0)
_EMA_SLOW: float = 1.0 - math.exp(-1.0 / 64.0)
_EMA_OUTPUT: float = 1.0 - math.exp(-1.0 / 16.0)
_SIGNAL_CLIP: float = 2.0
_WARMUP_TICKS: int = 128

_MANIFEST = AlphaManifest(
    alpha_id="mldm_depth_momentum",
    hypothesis=(
        "Deep-book (L2-L5) depth withdrawal precedes informed price moves. "
        "Net depth change at L2-L5 captures cancellation-driven adverse "
        "selection invisible to L1 OFI.  Orthogonal to L1 features by "
        "construction (excludes L1 entirely)."
    ),
    formula=(
        "deep_delta = sum(delta_bid[2:5]) - sum(delta_ask[2:5]); "
        "MLDM = EMA_fast(deep_delta) - EMA_slow(deep_delta), clipped [-2,2]; "
        "zeroed when BBO price changes (level-shift guard)"
    ),
    paper_refs=("arXiv:2306.05479", "arXiv:2502.18625", "arXiv:2112.13213"),
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


class MultiLevelDepthMomentumAlpha:
    """O(1) L2-L5 depth momentum detector.

    Requires bids/asks keyword args with multi-level depth data (shape (N,2), N>=2).
    Only uses levels 2-5 (index 1-4); L1 is excluded by design.
    Guards against BBO price shifts that cause spurious L2-L5 deltas.
    """

    __slots__ = (
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_prev_best_bid_px",
        "_prev_best_ask_px",
        "_cur_bid_qty",
        "_cur_ask_qty",
        "_deep_ema_fast",
        "_deep_ema_slow",
        "_output_ema",
        "_signal",
        "_initialized",
        "_tick_count",
    )

    def __init__(self) -> None:
        self._prev_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_best_bid_px: float = 0.0
        self._prev_best_ask_px: float = 0.0
        self._cur_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._deep_ema_fast: float = 0.0
        self._deep_ema_slow: float = 0.0
        self._output_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: object) -> float:
        cur_bid = self._cur_bid_qty
        cur_ask = self._cur_ask_qty
        cur_bid[:] = 0.0
        cur_ask[:] = 0.0

        if "bids" not in kwargs or "asks" not in kwargs:
            raise ValueError(
                "mldm_depth_momentum requires bids= and asks= keyword args "
                "with multi-level depth data (shape (N,2), N >= 2)."
            )

        bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
        asks = np.asarray(kwargs["asks"], dtype=np.float64).reshape(-1, 2)
        n_bid = min(bids.shape[0], _N_LEVELS)
        n_ask = min(asks.shape[0], _N_LEVELS)

        cur_bid[:n_bid] = bids[:n_bid, 1]
        cur_ask[:n_ask] = asks[:n_ask, 1]
        cur_best_bid_px = float(bids[0, 0]) if n_bid > 0 else 0.0
        cur_best_ask_px = float(asks[0, 0]) if n_ask > 0 else 0.0

        self._tick_count += 1

        # Thin LOB guard (Execution): need at least 2 levels for L2+ signal
        thin_book = n_bid < _MIN_LEVELS or n_ask < _MIN_LEVELS

        # BBO-shift guard (Execution): when best price changes, L2-L5 indices
        # shift causing spurious deltas. Zero the input for this tick.
        bbo_shifted = False
        if self._initialized:
            bbo_shifted = (
                cur_best_bid_px != self._prev_best_bid_px
                or cur_best_ask_px != self._prev_best_ask_px
            )

        delta_bid = cur_bid - self._prev_bid_qty
        delta_ask = cur_ask - self._prev_ask_qty

        np.copyto(self._prev_bid_qty, cur_bid)
        np.copyto(self._prev_ask_qty, cur_ask)
        self._prev_best_bid_px = cur_best_bid_px
        self._prev_best_ask_px = cur_best_ask_px

        if thin_book or bbo_shifted:
            deep_net = 0.0
        else:
            deep_delta_bid: float = 0.0
            deep_delta_ask: float = 0.0
            for i in range(_L2_START, min(_L2_END, n_bid)):
                deep_delta_bid += float(delta_bid[i])
            for i in range(_L2_START, min(_L2_END, n_ask)):
                deep_delta_ask += float(delta_ask[i])
            deep_net = deep_delta_bid - deep_delta_ask

        if not self._initialized:
            self._deep_ema_fast = deep_net
            self._deep_ema_slow = deep_net
            self._output_ema = 0.0
            self._initialized = True
        else:
            self._deep_ema_fast += _EMA_FAST * (deep_net - self._deep_ema_fast)
            self._deep_ema_slow += _EMA_SLOW * (deep_net - self._deep_ema_slow)

        raw_momentum = self._deep_ema_fast - self._deep_ema_slow
        self._output_ema += _EMA_OUTPUT * (raw_momentum - self._output_ema)

        if self._tick_count < _WARMUP_TICKS:
            self._signal = 0.0
        else:
            self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._output_ema))
        return self._signal

    def reset(self) -> None:
        self._prev_bid_qty[:] = 0.0
        self._prev_ask_qty[:] = 0.0
        self._prev_best_bid_px = 0.0
        self._prev_best_ask_px = 0.0
        self._cur_bid_qty[:] = 0.0
        self._cur_ask_qty[:] = 0.0
        self._deep_ema_fast = 0.0
        self._deep_ema_slow = 0.0
        self._output_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._tick_count = 0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = MultiLevelDepthMomentumAlpha

__all__ = ["MultiLevelDepthMomentumAlpha", "ALPHA_CLASS"]
