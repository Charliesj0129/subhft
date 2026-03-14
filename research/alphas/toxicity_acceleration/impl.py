"""Toxicity Acceleration Alpha — rate-of-change of informed flow toxicity.

Signal: clip((EMA_8(raw_tox) - EMA_64(raw_tox)) / max(EMA_64(raw_tox), eps) * sign(ofi), -2, 2)
Detects accelerating informed trading via dual-EMA toxicity divergence.

Papers 129 + 132: Cartea et al. "Detecting Toxic Flow" + "Brokers and Informed Traders".

Allocator Law: __slots__ on class; all state is scalar.
Precision Law: output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
# EMA decay: window ~ 64 ticks -> alpha = 1 - exp(-1/64) ~ 0.0155
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="toxicity_acceleration",
    hypothesis=(
        "When short-term toxicity (EMA-8) exceeds long-term toxicity (EMA-64),"
        " informed traders are accelerating their activity; following the OFI"
        " direction during acceleration predicts short-term price movement."
    ),
    formula=(
        "signal = clip((EMA_8(|QI|*spread_norm) - EMA_64(|QI|*spread_norm))"
        " / max(EMA_64(|QI|*spread_norm), eps) * sign(ofi_l1_ema8), -2, 2)"
    ),
    paper_refs=("129", "132"),
    data_fields=("bid_qty", "ask_qty", "spread_scaled", "ofi_l1_ema8"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class ToxicityAccelerationAlpha:
    """O(1) toxicity acceleration signal with dual-EMA divergence.

    update() accepts either:
      - 4 positional args:  bid_qty, ask_qty, spread_scaled, ofi_l1_ema8
      - keyword args:       bid_qty=..., ask_qty=..., spread_scaled=..., ofi_l1_ema8=...
    """

    __slots__ = (
        "_spread_base_ema",
        "_tox_fast",
        "_tox_slow",
        "_signal",
        "_initialized",
        "_tox_fast_initialized",
    )

    def __init__(self) -> None:
        self._spread_base_ema: float = 0.0
        self._tox_fast: float = 0.0
        self._tox_slow: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tox_fast_initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one tick of the toxicity acceleration signal.

        Args:
            bid_qty: Best-bid queue size.
            ask_qty: Best-ask queue size.
            spread_scaled: Spread in scaled-int units.
            ofi_l1_ema8: EMA-8 smoothed order flow imbalance (L1).

        Returns:
            The updated signal value.
        """
        # --- resolve inputs from various call conventions ---
        if len(args) >= 4:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
            spread_scaled = float(args[2])
            ofi_l1_ema8 = float(args[3])
        elif args:
            raise ValueError(
                "update() requires 4 positional args"
                " (bid_qty, ask_qty, spread_scaled, ofi_l1_ema8)"
                " or keyword args"
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))
            ofi_l1_ema8 = float(kwargs.get("ofi_l1_ema8", 0.0))

        # --- queue imbalance ratio ---
        denom = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(denom, 1.0)

        # --- spread normalization relative to EMA-64 baseline ---
        if not self._initialized:
            self._spread_base_ema = spread_scaled
            self._initialized = True
        else:
            self._spread_base_ema += _EMA_ALPHA_64 * (
                spread_scaled - self._spread_base_ema
            )

        spread_base = max(self._spread_base_ema, 1.0)
        spread_norm = spread_scaled / spread_base

        # --- raw toxicity = |QI| x spread_norm ---
        raw_tox = abs(qi) * spread_norm

        # --- dual-EMA toxicity tracking ---
        if not self._tox_fast_initialized:
            self._tox_fast = raw_tox
            self._tox_slow = raw_tox
            self._tox_fast_initialized = True
        else:
            self._tox_fast += _EMA_ALPHA_8 * (raw_tox - self._tox_fast)
            self._tox_slow += _EMA_ALPHA_64 * (raw_tox - self._tox_slow)

        # --- acceleration = (fast - slow) / max(slow, eps) ---
        tox_accel = (self._tox_fast - self._tox_slow) / max(
            self._tox_slow, _EPSILON
        )

        # --- directional signal: follow the informed flow ---
        if ofi_l1_ema8 == 0.0:
            self._signal = 0.0
        else:
            raw_signal = math.copysign(tox_accel, ofi_l1_ema8)
            self._signal = max(-2.0, min(2.0, raw_signal))
        return self._signal

    def reset(self) -> None:
        self._spread_base_ema = 0.0
        self._tox_fast = 0.0
        self._tox_slow = 0.0
        self._signal = 0.0
        self._initialized = False
        self._tox_fast_initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = ToxicityAccelerationAlpha

__all__ = ["ToxicityAccelerationAlpha", "ALPHA_CLASS"]
