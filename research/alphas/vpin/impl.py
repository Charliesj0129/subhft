"""VPIN BVC Alpha — paper 134 (Easley, Lopez de Prado, O'Hara 2012).

Signal:  VPIN = (1/n) * sum_{i=1}^{n} |V^S_i - V^B_i| / V_bucket
Where:
    V^B_τ = V_τ * Φ((P_τ - P_{τ-1}) / σ_ΔP)   (Bulk Volume Classification)
    V^S_τ = V_τ - V^B_τ
    σ_ΔP  = EMA_32(|ΔP|)

VPIN measures flow toxicity (probability of informed trading) via
volume-synchronized buckets and bulk volume classification.  Higher VPIN
signals more informed/toxic flow.

Allocator Law  : __slots__ on class; pre-allocated numpy ring buffer for buckets.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Cache Law      : numpy array for bucket imbalances (contiguous memory).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay for sigma estimator: window ≈ 32 ticks → α = 1 − exp(−1/32)
_EMA_ALPHA_32: float = 1.0 - math.exp(-1.0 / 32.0)
_EPSILON: float = 1e-12  # guards against division by zero
_SQRT2: float = math.sqrt(2.0)
_N_BUCKETS: int = 50
_BUCKET_SIZE: float = 1000.0

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="vpin_bvc",
    hypothesis=(
        "Volume-synchronized probability of informed trading (VPIN) using"
        " bulk volume classification detects flow toxicity: high VPIN"
        " indicates elevated adverse selection risk and predicts"
        " short-term liquidity deterioration."
    ),
    formula="VPIN = (1/n) * sum |V^S_i - V^B_i| / V_bucket",
    paper_refs=("134",),
    data_fields=("mid_price", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class VpinBvcAlpha:
    """O(1) VPIN estimator with bulk volume classification.

    update() accepts either:
      - 2 positional args:  mid_price, volume
      - keyword args:       mid_price=..., volume=...
    """

    __slots__ = (
        "_buckets_imbalance",
        "_n_filled",
        "_bucket_idx",
        "_current_buy",
        "_current_sell",
        "_current_vol",
        "_bucket_size",
        "_prev_mid",
        "_sigma_dp",
        "_signal",
        "_initialized",
    )

    def __init__(
        self,
        n_buckets: int = _N_BUCKETS,
        bucket_size: float = _BUCKET_SIZE,
    ) -> None:
        self._buckets_imbalance: np.ndarray = np.zeros(n_buckets, dtype=np.float64)
        self._n_filled: int = 0
        self._bucket_idx: int = 0
        self._current_buy: float = 0.0
        self._current_sell: float = 0.0
        self._current_vol: float = 0.0
        self._bucket_size: float = bucket_size
        self._prev_mid: float = 0.0
        self._sigma_dp: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: C901
        """Ingest one tick of (mid_price, volume) and return current VPIN."""
        # --- resolve mid_price and volume ---
        if len(args) >= 2:
            mid_price = float(args[0])
            volume = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (mid_price, volume)"
                " or keyword args"
            )
        else:
            mid_price = float(kwargs.get("mid_price", 0.0))
            volume = float(kwargs.get("volume", 0.0))

        # --- delta price and sigma update ---
        if not self._initialized:
            delta_mid = 0.0
            self._prev_mid = mid_price
            self._initialized = True
        else:
            delta_mid = mid_price - self._prev_mid
            self._prev_mid = mid_price

        abs_delta = abs(delta_mid)
        self._sigma_dp += _EMA_ALPHA_32 * (abs_delta - self._sigma_dp)

        # --- BVC: buy fraction via normal CDF ---
        sigma = max(self._sigma_dp, _EPSILON)
        z = delta_mid / sigma
        buy_frac = 0.5 * math.erfc(-z / _SQRT2)

        # --- accumulate into current bucket ---
        self._current_buy += volume * buy_frac
        self._current_sell += volume * (1.0 - buy_frac)
        self._current_vol += volume

        # --- bucket rotation ---
        n_buckets = len(self._buckets_imbalance)
        while self._current_vol >= self._bucket_size:
            overflow = self._current_vol - self._bucket_size
            # Scale down the overflow proportionally
            if self._current_vol > _EPSILON:
                frac_in = self._bucket_size / self._current_vol
            else:
                frac_in = 1.0
            buy_in = self._current_buy * frac_in
            sell_in = self._current_sell * frac_in

            self._buckets_imbalance[self._bucket_idx] = (
                abs(buy_in - sell_in) / self._bucket_size
            )

            self._bucket_idx = (self._bucket_idx + 1) % n_buckets
            if self._n_filled < n_buckets:
                self._n_filled += 1

            # Carry over the overflow into next bucket
            frac_out = 1.0 - frac_in
            self._current_buy = self._current_buy * frac_out
            self._current_sell = self._current_sell * frac_out
            self._current_vol = overflow

        # --- compute VPIN as mean of filled buckets ---
        if self._n_filled > 0:
            self._signal = float(
                np.sum(self._buckets_imbalance[: self._n_filled]) / self._n_filled
            )
            # Clamp to [0, 1] — theoretically bounded but guard floating point
            if self._signal < 0.0:
                self._signal = 0.0
            elif self._signal > 1.0:
                self._signal = 1.0
        else:
            self._signal = 0.0

        return self._signal

    def reset(self) -> None:
        self._buckets_imbalance[:] = 0.0
        self._n_filled = 0
        self._bucket_idx = 0
        self._current_buy = 0.0
        self._current_sell = 0.0
        self._current_vol = 0.0
        self._prev_mid = 0.0
        self._sigma_dp = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = VpinBvcAlpha

__all__ = ["VpinBvcAlpha", "ALPHA_CLASS"]
