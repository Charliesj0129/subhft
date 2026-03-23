"""OFI Depth Divergence Alpha — shallow vs deep lead-lag detection.

Signal:  shallow_momentum - deep_momentum  (variance-normalized per band)
Where:
    shallow_ofi = (delta_bid_L1 - delta_ask_L1 + delta_bid_L2 - delta_ask_L2) / 2
    deep_ofi    = (delta_bid_L3 - delta_ask_L3 + ... + delta_bid_L5 - delta_ask_L5) / 3
    shallow_momentum = EMA_fast(shallow_ofi) - EMA_slow(shallow_ofi)
    deep_momentum    = EMA_fast(deep_ofi) - EMA_slow(deep_ofi)
    signal = EMA_8(shallow_momentum - deep_momentum), clipped to [-2, 2]
    signal = 0.0 for first _WARMUP_TICKS ticks (slow EMA stabilization)

TWSE microstructure finding: On TWSE, deep-book activity (L3-L5) leading
shallow (L1-L2) indicates *liquidity provision* (passive limit orders being
replenished), NOT informed accumulation.  Informed flow on TWSE manifests
as aggressive L1-L2 activity leading depth — the "shallow leads deep"
pattern.  This is inverted relative to US equity (Cont et al. 2023) where
institutional informed traders hide orders at depth.

Band normalization: divide each band's OFI by its level count (2 for shallow,
3 for deep) to equalize variance.  Without this, the 3-level deep band would
have ~sqrt(3/2) higher variance than the 2-level shallow band.

Paper refs:
  Cont, Cucuringu, Zhang (2023) arXiv:2112.13213 — multi-level OFI integration.
  Muhle-Karbe, Rosenbaum et al. (2026) arXiv:2601.23172 — core/reaction flow.

Allocator Law  : __slots__ on class; prev-depth in pre-allocated numpy arrays.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Cache Law      : prev_{bid,ask}_qty are contiguous float64 arrays (5 elements each).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_N_LEVELS: int = 5
_EMA_FAST: float = 1.0 - math.exp(-1.0 / 4.0)
_EMA_SLOW: float = 1.0 - math.exp(-1.0 / 32.0)
_EMA_OUTPUT: float = 1.0 - math.exp(-1.0 / 8.0)
_SIGNAL_CLIP: float = 2.0
_SHALLOW_LEVELS: int = 2
_DEEP_LEVELS: int = 3
_INV_SHALLOW: float = 1.0 / _SHALLOW_LEVELS
_INV_DEEP: float = 1.0 / _DEEP_LEVELS
_WARMUP_TICKS: int = 64

_MANIFEST = AlphaManifest(
    alpha_id="ofi_depth_divergence",
    hypothesis=(
        "On TWSE, shallow-leading-deep OFI divergence detects informed "
        "aggression: when L1-L2 momentum exceeds L3-L5, informed traders "
        "are driving price at the top of book while depth replenishes "
        "passively.  Orthogonal to multilevel_ofi (ref 124) which blends "
        "all levels into one weighted sum."
    ),
    formula=(
        "signal = EMA_8(shallow_momentum - deep_momentum), "
        "where momentum = EMA_4(ofi_band/N_levels) - EMA_32(ofi_band/N_levels)"
    ),
    paper_refs=("arXiv:2112.13213", "arXiv:2601.23172"),
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


class OfiDepthDivergenceAlpha:
    """O(1) shallow-deep OFI lead-lag divergence detector.

    Requires bids/asks keyword args with multi-level depth data.
    L1-only fallback raises ValueError.
    """

    __slots__ = (
        "_prev_bid_qty", "_prev_ask_qty", "_cur_bid_qty", "_cur_ask_qty",
        "_shallow_ema_fast", "_shallow_ema_slow",
        "_deep_ema_fast", "_deep_ema_slow",
        "_output_ema", "_signal", "_initialized", "_tick_count",
    )

    def __init__(self) -> None:
        self._prev_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._shallow_ema_fast: float = 0.0
        self._shallow_ema_slow: float = 0.0
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

        if "bids" in kwargs and "asks" in kwargs:
            bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
            asks = np.asarray(kwargs["asks"], dtype=np.float64).reshape(-1, 2)
            n_bid = min(bids.shape[0], _N_LEVELS)
            n_ask = min(asks.shape[0], _N_LEVELS)
            cur_bid[:n_bid] = bids[:n_bid, 1]
            cur_ask[:n_ask] = asks[:n_ask, 1]
        else:
            raise ValueError(
                "ofi_depth_divergence requires bids= and asks= keyword args "
                "with multi-level depth data (shape (N,2), N >= 3). "
                "L1-only fallback is not supported — use multilevel_ofi instead."
            )

        self._tick_count += 1
        delta_bid = cur_bid - self._prev_bid_qty
        delta_ask = cur_ask - self._prev_ask_qty
        ofi_per_level = delta_bid - delta_ask

        np.copyto(self._prev_bid_qty, cur_bid)
        np.copyto(self._prev_ask_qty, cur_ask)

        shallow_ofi = (float(ofi_per_level[0]) + float(ofi_per_level[1])) * _INV_SHALLOW
        deep_ofi = (float(ofi_per_level[2]) + float(ofi_per_level[3]) + float(ofi_per_level[4])) * _INV_DEEP

        if not self._initialized:
            self._shallow_ema_fast = shallow_ofi
            self._shallow_ema_slow = shallow_ofi
            self._deep_ema_fast = deep_ofi
            self._deep_ema_slow = deep_ofi
            self._output_ema = 0.0
            self._initialized = True
        else:
            self._shallow_ema_fast += _EMA_FAST * (shallow_ofi - self._shallow_ema_fast)
            self._shallow_ema_slow += _EMA_SLOW * (shallow_ofi - self._shallow_ema_slow)
            self._deep_ema_fast += _EMA_FAST * (deep_ofi - self._deep_ema_fast)
            self._deep_ema_slow += _EMA_SLOW * (deep_ofi - self._deep_ema_slow)

        shallow_momentum = self._shallow_ema_fast - self._shallow_ema_slow
        deep_momentum = self._deep_ema_fast - self._deep_ema_slow
        raw_divergence = shallow_momentum - deep_momentum

        self._output_ema += _EMA_OUTPUT * (raw_divergence - self._output_ema)

        if self._tick_count < _WARMUP_TICKS:
            self._signal = 0.0
        else:
            self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._output_ema))
        return self._signal

    def reset(self) -> None:
        self._prev_bid_qty[:] = 0.0
        self._prev_ask_qty[:] = 0.0
        self._cur_bid_qty[:] = 0.0
        self._cur_ask_qty[:] = 0.0
        self._shallow_ema_fast = 0.0
        self._shallow_ema_slow = 0.0
        self._deep_ema_fast = 0.0
        self._deep_ema_slow = 0.0
        self._output_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._tick_count = 0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = OfiDepthDivergenceAlpha

__all__ = ["OfiDepthDivergenceAlpha", "ALPHA_CLASS"]
