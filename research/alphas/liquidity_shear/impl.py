"""Directional Liquidity Geometric Shear Alpha — ref 032.

Signal: Measures asymmetry in the LOB depth profile shape between bid
and ask sides.  When bid-side depth decays faster than ask-side (shear),
it signals selling pressure.  The "geometric shear" is the log-ratio
of bid vs ask depth decay rates.

Formula:
  depth_slope_k = total_qty / weighted_position_sum
                = Σ qty_k / Σ (k · qty_k)
  shear = log(ask_depth_slope / bid_depth_slope)
  signal = EMA_8(shear)

Positive shear → ask side steeper (concentrated near BBO) → buying pressure.
Negative shear → bid side steeper → selling pressure.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks → alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_EPSILON: float = 1e-12  # guards against division by zero / log(0)
_SIGNAL_CLIP: float = 2.0  # signal bounded to [-2, 2]

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="liquidity_shear",
    hypothesis=(
        "Asymmetric depth profile decay between bid and ask sides predicts "
        "short-term price direction: steeper ask-side depth signals buying "
        "pressure."
    ),
    formula="signal = EMA_8(log(ask_depth_slope / bid_depth_slope))",
    paper_refs=("032",),
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


def _depth_slope(book_side: object) -> float:
    """Compute depth slope = total_qty / weighted_position_sum.

    ``book_side`` is array-like of shape (N, 2) where columns are
    [price, qty].  Position index k is 1-based (closest to mid = 1).

    Returns 0.0 when the book side is empty or total qty is zero.
    """
    import numpy as np  # lazy import; not on production hot path

    arr = np.asarray(book_side, dtype=np.float64).reshape(-1, 2)
    n = arr.shape[0]
    if n == 0:
        return 0.0

    qtys = arr[:, 1]
    total_qty = qtys.sum()
    if total_qty <= _EPSILON:
        return 0.0

    # k = 1, 2, ..., N (1-based position from BBO)
    positions = np.arange(1, n + 1, dtype=np.float64)
    weighted_sum = (positions * qtys).sum()

    if weighted_sum <= _EPSILON:
        return 0.0

    return float(total_qty / weighted_sum)


class LiquidityShearAlpha:
    """O(N) directional liquidity shear predictor with EMA smoothing.

    update() accepts keyword args ``bids`` and ``asks`` (np.ndarray shape (N,2))
    or falls back to ``bid_qty``/``ask_qty`` scalars (returning neutral signal
    since depth profile is unavailable).
    """

    __slots__ = ("_shear_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._shear_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: object, **kwargs: object) -> float:
        """Update state with new LOB snapshot and return current signal.

        Accepts:
          - keyword: bids=np.ndarray(N,2), asks=np.ndarray(N,2)
          - positional fallback: bid_qty, ask_qty (no depth info → neutral)
        """
        bids = kwargs.get("bids")
        asks = kwargs.get("asks")

        if bids is not None and asks is not None:
            bid_slope = _depth_slope(bids)
            ask_slope = _depth_slope(asks)

            if bid_slope <= _EPSILON or ask_slope <= _EPSILON:
                raw_shear = 0.0
            else:
                raw_shear = math.log(ask_slope / bid_slope)
        else:
            # No depth info available — return neutral shear
            raw_shear = 0.0

        if not self._initialized:
            self._shear_ema = raw_shear
            self._initialized = True
        else:
            self._shear_ema += _EMA_ALPHA * (raw_shear - self._shear_ema)

        # Clip to [-2, 2]
        self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._shear_ema))
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._shear_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = LiquidityShearAlpha

__all__ = ["LiquidityShearAlpha", "ALPHA_CLASS"]
