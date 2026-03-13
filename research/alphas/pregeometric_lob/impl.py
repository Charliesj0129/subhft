"""Pregeometric LOB Liquidity Origins Alpha — ref 092.

Signal: Fits a Gamma distribution to the LOB depth profile via method of
moments and uses the shape-parameter asymmetry between bid/ask as a signal.
When the ask-side Gamma shape is steeper than bid-side, concentrated selling
pressure indicates bearish pressure.

Formula:
  gamma_shape(side) = mean(qty)^2 / var(qty)   (method-of-moments estimator)
  signal = EMA_8(gamma_shape_bid - gamma_shape_ask)  ∈ [-2, 2]

Allocator Law  : __slots__ on class; all state is scalar (no heap per tick).
Precision Law  : output is float (signal score, not price — no Decimal needed).
Cache Law      : numpy operations on contiguous qty arrays.
"""
from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="pregeometric_lob",
    hypothesis=(
        "Gamma distribution shape parameter of LOB depth reveals liquidity "
        "concentration: concentrated bid-side support signals upward price "
        "pressure."
    ),
    formula="signal = EMA_8(gamma_shape_bid - gamma_shape_ask)",
    paper_refs=("092",),
    data_fields=("bids", "asks"),
    complexity="O(N)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.ENSEMBLE,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
    feature_set_version="lob_shared_v1",
)


class PregeometricLobAlpha:
    """Gamma-shape LOB depth asymmetry alpha.

    update() accepts:
      - keyword args: bids=np.ndarray (N,2), asks=np.ndarray (N,2)
      - positional args: bid_qty, ask_qty (scalar fallback, returns 0 signal)

    O(N) per tick where N = number of depth levels.
    """

    __slots__ = ("_shape_diff_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._shape_diff_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @staticmethod
    def _estimate_gamma_shape(quantities: np.ndarray) -> float:
        """Method-of-moments Gamma shape estimator: k = mean^2 / var.

        Returns 1.0 (neutral) for degenerate inputs (< 2 levels or zero var).
        """
        if len(quantities) < 2:
            return 1.0
        mean_q = float(np.mean(quantities))
        var_q = float(np.var(quantities))
        if var_q < 1e-10:
            return 1.0
        return mean_q * mean_q / var_q

    def update(self, *args: float, **kwargs: object) -> float:
        """Update state and return the current signal.

        Accepts bids/asks arrays (primary) or bid_qty/ask_qty scalars (fallback).
        """
        bids = kwargs.get("bids")
        asks = kwargs.get("asks")

        if bids is not None and asks is not None:
            bid_arr = np.asarray(bids)
            ask_arr = np.asarray(asks)

            # Handle empty arrays
            if bid_arr.size == 0 or ask_arr.size == 0:
                return self._signal

            bid_qtys = bid_arr.reshape(-1, 2)[:, 1]
            ask_qtys = ask_arr.reshape(-1, 2)[:, 1]

            bid_shape = self._estimate_gamma_shape(bid_qtys)
            ask_shape = self._estimate_gamma_shape(ask_qtys)

            # Positive when bid more concentrated -> bid support stronger
            shape_diff = bid_shape - ask_shape

            if not self._initialized:
                self._shape_diff_ema = shape_diff
                self._initialized = True
            else:
                self._shape_diff_ema += _EMA_ALPHA * (
                    shape_diff - self._shape_diff_ema
                )

            self._signal = max(-2.0, min(2.0, self._shape_diff_ema))
            return self._signal

        # Fallback: positional bid_qty/ask_qty or keyword — no depth info
        if len(args) >= 2:
            # Scalar fallback: no depth profile, signal stays at current value
            return self._signal
        if "bid_qty" in kwargs or "ask_qty" in kwargs:
            return self._signal

        # No args at all — return current signal
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state."""
        self._shape_diff_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = PregeometricLobAlpha

__all__ = ["PregeometricLobAlpha", "ALPHA_CLASS"]
