"""Toxic Flow Alpha — VPIN-inspired flow toxicity indicator.

Signal: sign(ofi_ema8) x EMA_8(|QI| x spread_norm)
Detects informed trading via spread-OFI interaction.

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
    alpha_id="toxic_flow",
    hypothesis=(
        "Simultaneous OFI surge and spread widening indicates toxic informed"
        " flow; following the informed direction is predictive of short-term"
        " price movement."
    ),
    formula=(
        "signal_t = sign(ofi_ema8)"
        " x EMA_8(|QI| x spread_scaled / max(EMA_64(spread_scaled), 1))"
    ),
    paper_refs=(),  # Inspired by Easley, Lopez de Prado & O'Hara (2012) VPIN
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


class ToxicFlowAlpha:
    """O(1) VPIN-inspired toxicity signal with EMA smoothing.

    update() accepts either:
      - 4 positional args:  bid_qty, ask_qty, spread_scaled, ofi_l1_ema8
      - keyword args:       bid_qty=..., ask_qty=..., spread_scaled=..., ofi_l1_ema8=...
    """

    __slots__ = (
        "_spread_base_ema",
        "_toxicity_ema",
        "_signal",
        "_initialized",
        "_toxicity_initialized",
    )

    def __init__(self) -> None:
        self._spread_base_ema: float = 0.0
        self._toxicity_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._toxicity_initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one tick of the toxic flow signal.

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
        qi = (bid_qty - ask_qty) / (denom + _EPSILON)
        ofi_abs = abs(qi)

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
        raw_toxicity = ofi_abs * spread_norm

        # --- EMA-8 smooth the toxicity ---
        if not self._toxicity_initialized:
            self._toxicity_ema = raw_toxicity
            self._toxicity_initialized = True
        else:
            self._toxicity_ema += _EMA_ALPHA_8 * (
                raw_toxicity - self._toxicity_ema
            )

        # --- directional signal: follow the informed flow ---
        if ofi_l1_ema8 == 0.0:
            self._signal = 0.0
        else:
            self._signal = math.copysign(self._toxicity_ema, ofi_l1_ema8)
        return self._signal

    def reset(self) -> None:
        self._spread_base_ema = 0.0
        self._toxicity_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._toxicity_initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = ToxicFlowAlpha

__all__ = ["ToxicFlowAlpha", "ALPHA_CLASS"]
