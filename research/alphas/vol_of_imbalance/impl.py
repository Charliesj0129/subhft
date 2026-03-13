"""Vol-of-Imbalance Alpha — ref 064 (volatility time ordering).

Signal:  Volatility of the queue imbalance signal itself measures regime
         uncertainty; high meta-vol signals trending regimes, low meta-vol
         signals mean-reverting regimes.

Formula:
  qi        = (bid - ask) / max(bid + ask, 1)
  qi_ema    += α8 * (qi - qi_ema)
  deviation = (qi - qi_ema)²
  vol       = sqrt( EMA_16(deviation) )
  baseline  += α64 * (vol - baseline)
  raw       = (vol - baseline) / max(baseline, ε)
  signal    = clip(raw * sign(qi_ema), -2, 2)

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_A8: float = 1.0 - math.exp(-1.0 / 8.0)  # ≈ 0.1175 — qi smoothing
_A16: float = 1.0 - math.exp(-1.0 / 16.0)  # ≈ 0.0606 — deviation smoothing
_A64: float = 1.0 - math.exp(-1.0 / 64.0)  # ≈ 0.0154 — vol baseline

_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="vol_of_imbalance",
    hypothesis=(
        "Volatility of the queue imbalance signal itself measures regime"
        " uncertainty; high meta-vol signals trending regimes, low meta-vol"
        " signals mean-reverting regimes."
    ),
    formula=(
        "signal = clip((sqrt(EMA16((qi - EMA8(qi))^2)) - EMA64(vol)) / max(EMA64(vol), eps) * sign(EMA8(qi)), -2, 2)"
    ),
    paper_refs=("064",),
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


class VolOfImbalanceAlpha:
    """O(1) vol-of-imbalance predictor with multi-EMA state.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
      - bids/asks arrays:   bids=np.ndarray (shape (N,2)), asks=np.ndarray (shape (N,2))
    """

    __slots__ = ("_qi_ema", "_dev_ema", "_vol_baseline", "_signal")

    def __init__(self) -> None:
        self._qi_ema: float = 0.0
        self._dev_ema: float = 0.0
        self._vol_baseline: float = 0.0
        self._signal: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state and return the current signal."""
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

        total = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(total, 1.0)

        # EMA of qi (directional)
        self._qi_ema += _A8 * (qi - self._qi_ema)

        # Squared deviation from qi_ema
        deviation = (qi - self._qi_ema) ** 2

        # EMA of deviation → variance proxy
        self._dev_ema += _A16 * (deviation - self._dev_ema)

        # Instantaneous vol = sqrt(smoothed variance)
        vol = math.sqrt(max(self._dev_ema, 0.0))

        # Slow baseline of vol
        self._vol_baseline += _A64 * (vol - self._vol_baseline)

        # Normalized excess vol
        raw = (vol - self._vol_baseline) / max(self._vol_baseline, _EPSILON)

        # Directional via sign of qi_ema
        sign_qi = 1.0 if self._qi_ema > 0.0 else (-1.0 if self._qi_ema < 0.0 else 0.0)
        self._signal = max(-2.0, min(2.0, raw * sign_qi))
        return self._signal

    def reset(self) -> None:
        """Clear all EMA state to zero."""
        self._qi_ema = 0.0
        self._dev_ema = 0.0
        self._vol_baseline = 0.0
        self._signal = 0.0

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = VolOfImbalanceAlpha

__all__ = ["VolOfImbalanceAlpha", "ALPHA_CLASS"]
