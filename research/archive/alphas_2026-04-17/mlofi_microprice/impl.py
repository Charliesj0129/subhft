"""MLOFI Microprice Correction Alpha — multi-level OFI microprice adjustment.

Signal:  micro_adj = alpha * MLOFI_integrated
Where:
    MLOFI_L_k = delta_bid_qty_k - delta_ask_qty_k  (per-level OFI, k=1..5)
    MLOFI_integrated = sum_{k=1}^{5} w_k * MLOFI_L_k
    w_k = lambda^(k-1), lambda in [0.3, 0.7]  (geometric weighting)
    alpha = regression coefficient (price units per MLOFI unit)
    micro_adj = EMA_8(MLOFI_integrated) * alpha

This alpha outputs the CORRECTION TERM (micro_adj) as a scaled integer,
representing the estimated microprice displacement from L1 mid in price
units (x10000).

Stage 2 findings (2026-03-27):
  - TXFD6: IC near zero at all horizons.  MLOFI has NO predictive power on
    TAIFEX front-month futures.  Kill gate IC(30s)=-0.020 (PASS by absolute
    value but wrong sign at short horizons — coefficient wildly unstable).
  - 2330 (TSMC equity): Strong signal IC +0.093 to +0.206 across 250ms-30s.
    Incremental IC over L1 = +0.12 at 30s.  Genuine multi-level value-add.
  - TWSE sign: POSITIVE alpha (not NEGATIVE as hypothesized).  MLOFI > 0
    predicts price UP, not DOWN.  Informed flow interpretation holds.
  - Recommendation: Viable ONLY on equities (2330).  NOT viable on TXFD6.

Paper refs:
  Cont, Cucuringu, Zhang (2023) arXiv:2112.13213 — multi-level OFI.
  Muhle-Karbe, Rosenbaum et al. (2026) arXiv:2601.23172 — core/reaction flow.

Allocator Law  : __slots__ on class; prev-depth in pre-allocated numpy arrays.
Precision Law  : output is int scaled x10000 (price correction in price units).
Cache Law      : prev_{bid,ask}_qty are contiguous float64 arrays (5 elements each).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_N_LEVELS: int = 5
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)  # EMA window=8
_WARMUP_TICKS: int = 64
_DEFAULT_LAMBDA: float = 0.5
_DEFAULT_ALPHA_COEF: float = 17.0  # Mean regression alpha from 2330 data (price_x10000 / MLOFI)
_SIGNAL_CLIP: float = 500.0  # Max correction in price units x10000

_MANIFEST = AlphaManifest(
    alpha_id="mlofi_microprice_correction",
    hypothesis=(
        "Multi-level OFI (MLOFI) with geometric weighting captures L2-L5 "
        "informed flow information that L1 microprice misses. The MLOFI "
        "correction term improves mid-price prediction at 2-30s horizons. "
        "Validated on 2330 (TSMC equity) with IC=+0.206 at 30s. "
        "NOT viable on TXFD6 (IC near zero, unstable coefficients)."
    ),
    formula=(
        "micro_adj = alpha * EMA_8(sum_{k=1..5} lambda^(k-1) * (delta_bid_k - delta_ask_k)), "
        "lambda=0.5, alpha=17.0 (regression coefficient from 2330 OLS)"
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
    feature_set_version="lob_shared_v2",
)


class MlofiMicropriceCorrectionAlpha:
    """O(1) MLOFI-driven microprice correction.

    Computes the multi-level OFI correction to L1 microprice.
    Output is the correction term in scaled int (x10000 price units).

    Requires bids/asks keyword args with L5 depth data.
    """

    __slots__ = (
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_cur_bid_qty",
        "_cur_ask_qty",
        "_prev_bid_price",
        "_prev_ask_price",
        "_cur_bid_price",
        "_cur_ask_price",
        "_weights",
        "_mlofi_ema",
        "_signal",
        "_initialized",
        "_tick_count",
        "_alpha_coef",
        "_lam",
    )

    def __init__(
        self,
        lam: float = _DEFAULT_LAMBDA,
        alpha_coef: float = _DEFAULT_ALPHA_COEF,
    ) -> None:
        self._prev_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_bid_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_ask_qty: np.ndarray = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_bid_price: np.ndarray = np.zeros(_N_LEVELS, dtype=np.int64)
        self._prev_ask_price: np.ndarray = np.zeros(_N_LEVELS, dtype=np.int64)
        self._cur_bid_price: np.ndarray = np.zeros(_N_LEVELS, dtype=np.int64)
        self._cur_ask_price: np.ndarray = np.zeros(_N_LEVELS, dtype=np.int64)
        self._weights: np.ndarray = np.array(
            [lam ** k for k in range(_N_LEVELS)], dtype=np.float64
        )
        self._mlofi_ema: float = 0.0
        self._signal: int = 0
        self._initialized: bool = False
        self._tick_count: int = 0
        self._alpha_coef: float = alpha_coef
        self._lam: float = lam

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: object) -> int:
        """Update with new L5 book state. Returns correction in scaled int x10000."""
        cur_bq = self._cur_bid_qty
        cur_aq = self._cur_ask_qty
        cur_bq[:] = 0.0
        cur_aq[:] = 0.0

        if "bids" not in kwargs or "asks" not in kwargs:
            raise ValueError(
                "mlofi_microprice_correction requires bids= and asks= keyword args "
                "with multi-level depth data (shape (N,2), N >= 2)."
            )

        bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
        asks = np.asarray(kwargs["asks"], dtype=np.float64).reshape(-1, 2)
        n_bid = min(bids.shape[0], _N_LEVELS)
        n_ask = min(asks.shape[0], _N_LEVELS)
        cur_bq[:n_bid] = bids[:n_bid, 1]
        cur_aq[:n_ask] = asks[:n_ask, 1]

        # Extract prices for BBO-shift guard (pre-allocated in __init__)
        cur_bp = self._cur_bid_price
        cur_ap = self._cur_ask_price
        cur_bp[:] = 0
        cur_ap[:] = 0
        cur_bp[:n_bid] = bids[:n_bid, 0].astype(np.int64)
        cur_ap[:n_ask] = asks[:n_ask, 0].astype(np.int64)

        self._tick_count += 1

        # BBO-shift guard: zero MLOFI when best prices change
        bbo_shifted = self._initialized and (
            cur_bp[0] != self._prev_bid_price[0]
            or cur_ap[0] != self._prev_ask_price[0]
        )

        if bbo_shifted or not self._initialized:
            mlofi_raw = 0.0
        else:
            # Per-level OFI with level-shift guard
            delta_bid = cur_bq - self._prev_bid_qty
            delta_ask = cur_aq - self._prev_ask_qty
            ofi = delta_bid - delta_ask

            # Zero levels where price changed
            for k in range(_N_LEVELS):
                if cur_bp[k] != self._prev_bid_price[k] or cur_ap[k] != self._prev_ask_price[k]:
                    ofi[k] = 0.0

            mlofi_raw = float(np.dot(ofi, self._weights))

        # Store current as previous
        np.copyto(self._prev_bid_qty, cur_bq)
        np.copyto(self._prev_ask_qty, cur_aq)
        self._prev_bid_price[:] = cur_bp
        self._prev_ask_price[:] = cur_ap

        if not self._initialized:
            self._initialized = True
            self._mlofi_ema = 0.0
        else:
            self._mlofi_ema += _EMA_ALPHA * (mlofi_raw - self._mlofi_ema)

        if self._tick_count < _WARMUP_TICKS:
            self._signal = 0
        else:
            correction = self._alpha_coef * self._mlofi_ema
            clipped = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, correction))
            self._signal = int(round(clipped))

        return self._signal

    def reset(self) -> None:
        """Reset all state for new session."""
        self._prev_bid_qty[:] = 0.0
        self._prev_ask_qty[:] = 0.0
        self._cur_bid_qty[:] = 0.0
        self._cur_ask_qty[:] = 0.0
        self._prev_bid_price[:] = 0
        self._prev_ask_price[:] = 0
        self._cur_bid_price[:] = 0
        self._cur_ask_price[:] = 0
        self._mlofi_ema = 0.0
        self._signal = 0
        self._initialized = False
        self._tick_count = 0

    def get_signal(self) -> int:
        """Return current correction value (scaled int x10000)."""
        return self._signal

    def get_mlofi_ema(self) -> float:
        """Return raw MLOFI EMA (for diagnostics)."""
        return self._mlofi_ema


ALPHA_CLASS = MlofiMicropriceCorrectionAlpha

__all__ = ["MlofiMicropriceCorrectionAlpha", "ALPHA_CLASS"]
