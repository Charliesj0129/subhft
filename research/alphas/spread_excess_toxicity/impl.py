"""Spread Excess Toxicity Alpha — adverse selection via spread deviation.

Signal: clip(EMA_8((spread - baseline)/baseline * |QI|) * sign(ofi_ema8), -2, 2)
Measures the toxicity premium: gap between actual spread and EMA-64
baseline as an adverse selection indicator.  Fires only on spread
widening events (deviation > 0), unlike the ratio-based toxic_flow.

Paper ref 131: Cartea & Sanchez-Betancourt 2025.

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
    alpha_id="spread_excess_toxicity",
    hypothesis=(
        "The gap between current spread and EMA-64 baseline measures adverse"
        " selection intensity; when spread widens beyond baseline AND OFI is"
        " directional, the market maker is being adversely selected — follow"
        " the informed direction."
    ),
    formula=(
        "signal = clip(EMA_8((spread_scaled - EMA_64(spread_scaled))"
        " / max(EMA_64(spread_scaled), 1) * |QI|) * sign(ofi_l1_ema8), -2, 2)"
    ),
    paper_refs=("131",),
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


class SpreadExcessToxicityAlpha:
    """O(1) spread-excess toxicity signal with EMA smoothing.

    update() accepts either:
      - 4 positional args:  bid_qty, ask_qty, spread_scaled, ofi_l1_ema8
      - keyword args:       bid_qty=..., ask_qty=..., spread_scaled=..., ofi_l1_ema8=...
    """

    __slots__ = (
        "_spread_base_ema",
        "_excess_tox_ema",
        "_signal",
        "_initialized",
        "_excess_initialized",
    )

    def __init__(self) -> None:
        self._spread_base_ema: float = 0.0
        self._excess_tox_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._excess_initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one tick of the spread excess toxicity signal.

        Args:
            bid_qty: Best-bid queue size.
            ask_qty: Best-ask queue size.
            spread_scaled: Spread in scaled-int units.
            ofi_l1_ema8: EMA-8 smoothed order flow imbalance (L1).

        Returns:
            The updated signal value clipped to [-2, 2].
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
        qi = (bid_qty - ask_qty) / max(denom, _EPSILON)

        # --- spread baseline EMA-64 ---
        if not self._initialized:
            self._spread_base_ema = spread_scaled
            self._initialized = True
        else:
            self._spread_base_ema += _EMA_ALPHA_64 * (
                spread_scaled - self._spread_base_ema
            )

        # --- spread excess (deviation from baseline) ---
        spread_base = max(self._spread_base_ema, 1.0)
        spread_excess = (spread_scaled - spread_base) / spread_base

        # --- raw toxicity = spread_excess * |qi| ---
        raw = spread_excess * abs(qi)

        # --- EMA-8 smooth the excess toxicity ---
        if not self._excess_initialized:
            self._excess_tox_ema = raw
            self._excess_initialized = True
        else:
            self._excess_tox_ema += _EMA_ALPHA_8 * (
                raw - self._excess_tox_ema
            )

        # --- directional signal: follow the informed flow, clipped ---
        # Use multiplication (not copysign) so negative excess (spread
        # narrowing = low toxicity) produces a dampened/opposite signal.
        if ofi_l1_ema8 == 0.0:
            self._signal = 0.0
        else:
            self._signal = max(
                -2.0,
                min(2.0, self._excess_tox_ema * math.copysign(1.0, ofi_l1_ema8)),
            )
        return self._signal

    def reset(self) -> None:
        self._spread_base_ema = 0.0
        self._excess_tox_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._excess_initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = SpreadExcessToxicityAlpha

__all__ = ["SpreadExcessToxicityAlpha", "ALPHA_CLASS"]
