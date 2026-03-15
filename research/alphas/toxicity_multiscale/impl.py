"""Toxicity Multiscale Alpha — volatility-weighted queue toxicity.

Signal: clip(sign(QI) * EMA_8(EMA_16(|d_mid|) * |QI| * spread_dev), -2, 2)
where spread_dev = spread_scaled / max(EMA_64(spread_scaled), 1)

Combines three microstructure dimensions:
1. Queue imbalance magnitude (|QI|) — informed flow pressure
2. Mid-price volatility (EMA_16(|delta_mid|)) — price impact
3. Spread deviation (spread / baseline) — market maker retreat

Directional: follows sign of queue imbalance.

Papers 129 + 132: Cartea et al. multi-scale toxicity indicators.

Allocator Law: __slots__ on class; all state is scalar.
Precision Law: output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants: alpha = 1 - exp(-1/N)
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="toxicity_multiscale",
    hypothesis=(
        "When mid-price volatility, queue imbalance, and spread widening"
        " coincide, it indicates multi-dimensional toxic flow; following"
        " the imbalance direction predicts short-term price movement."
    ),
    formula=(
        "signal = clip(sign(QI)"
        " * EMA_8(EMA_16(|d_mid|) * |QI| * spread_scaled/max(EMA_64(spread_scaled),1)),"
        " -2, 2)"
    ),
    paper_refs=("129", "132"),
    data_fields=("bid_qty", "ask_qty", "spread_scaled", "mid_price"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class ToxicityMultiscaleAlpha:
    """O(1) multiscale toxicity signal combining volatility, QI, and spread.

    update() accepts either:
      - 4 positional args:  bid_qty, ask_qty, spread_scaled, mid_price
      - keyword args:       bid_qty=..., ask_qty=..., spread_scaled=..., mid_price=...
    """

    __slots__ = (
        "_prev_mid",
        "_vol_ema",
        "_spread_base_ema",
        "_tox_ema",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._vol_ema: float = 0.0
        self._spread_base_ema: float = 0.0
        self._tox_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one tick of the multiscale toxicity signal.

        Args:
            bid_qty: Best-bid queue size.
            ask_qty: Best-ask queue size.
            spread_scaled: Spread in scaled-int units.
            mid_price: Mid price (float).

        Returns:
            The updated signal value clipped to [-2, 2].
        """
        # --- resolve inputs from various call conventions ---
        if len(args) >= 4:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
            spread_scaled = float(args[2])
            mid_price = float(args[3])
        elif args:
            raise ValueError(
                "update() requires 4 positional args"
                " (bid_qty, ask_qty, spread_scaled, mid_price)"
                " or keyword args"
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))
            mid_price = float(kwargs.get("mid_price", 0.0))

        # --- queue imbalance ratio ---
        denom = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(denom, 1.0)

        # --- initialization ---
        if not self._initialized:
            self._prev_mid = mid_price
            self._vol_ema = 0.0
            self._spread_base_ema = spread_scaled
            self._tox_ema = 0.0
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # --- mid-price volatility: EMA_16(|delta_mid|) ---
        d_mid = abs(mid_price - self._prev_mid)
        self._prev_mid = mid_price
        self._vol_ema += _EMA_ALPHA_16 * (d_mid - self._vol_ema)

        # --- spread deviation: spread / max(EMA_64(spread), 1) ---
        self._spread_base_ema += _EMA_ALPHA_64 * (
            spread_scaled - self._spread_base_ema
        )
        spread_base = max(self._spread_base_ema, 1.0)
        spread_dev = spread_scaled / spread_base

        # --- raw multiscale toxicity: volatility * |QI| * spread_dev ---
        raw_tox = self._vol_ema * abs(qi) * spread_dev

        # --- EMA-8 smooth the toxicity ---
        self._tox_ema += _EMA_ALPHA_8 * (raw_tox - self._tox_ema)

        # --- directional signal: follow the queue imbalance ---
        if qi == 0.0:
            self._signal = 0.0
        else:
            raw_signal = math.copysign(self._tox_ema, qi)
            self._signal = max(-2.0, min(2.0, raw_signal))
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._vol_ema = 0.0
        self._spread_base_ema = 0.0
        self._tox_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = ToxicityMultiscaleAlpha

__all__ = ["ToxicityMultiscaleAlpha", "ALPHA_CLASS"]
