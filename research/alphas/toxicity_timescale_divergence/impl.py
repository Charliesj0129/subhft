"""Toxicity Timescale Divergence Alpha — Cartea et al. (Papers 129+132).

Signal: clip((EMA_4(QI) - EMA_32(QI)) * spread_gate, -1, 1)
Captures divergence between fast and slow queue imbalance EMA,
gated by spread excess — identifying informed flow the market maker
hasn't adjusted for.

Allocator Law: __slots__ on class; all state is scalar.
Precision Law: output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants: alpha = 1 - exp(-1/N)
_EMA_ALPHA_4: float = 1.0 - math.exp(-1.0 / 4.0)
_EMA_ALPHA_32: float = 1.0 - math.exp(-1.0 / 32.0)
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="toxicity_timescale_divergence",
    hypothesis=(
        "Divergence between fast (EMA-4) and slow (EMA-32) queue imbalance,"
        " gated by spread excess, identifies informed flow the market maker"
        " hasn't adjusted for; the spread gate acts as a confidence filter."
    ),
    formula=(
        "signal = clip((EMA_4(QI) - EMA_32(QI))"
        " * min(max(spread_scaled/EMA_64(spread_scaled) - 1, 0) + 0.1, 1),"
        " -1, 1)"
    ),
    paper_refs=("129", "132"),
    data_fields=("bid_qty", "ask_qty", "spread_scaled"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class ToxicityTimescaleDivergenceAlpha:
    """O(1) fast/slow QI divergence signal with spread gating.

    update() accepts either:
      - 3 positional args:  bid_qty, ask_qty, spread_scaled
      - keyword args:       bid_qty=..., ask_qty=..., spread_scaled=...
    """

    __slots__ = (
        "_qi_fast",
        "_qi_slow",
        "_spread_base",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._qi_fast: float = 0.0
        self._qi_slow: float = 0.0
        self._spread_base: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one tick of the toxicity timescale divergence signal.

        Args:
            bid_qty: Best-bid queue size.
            ask_qty: Best-ask queue size.
            spread_scaled: Spread in scaled-int units.

        Returns:
            The updated signal value clipped to [-1, 1].
        """
        # --- resolve inputs from various call conventions ---
        if len(args) >= 3:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
            spread_scaled = float(args[2])
        elif args:
            raise ValueError(
                "update() requires 3 positional args"
                " (bid_qty, ask_qty, spread_scaled)"
                " or keyword args"
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))

        # --- queue imbalance ratio ---
        denom = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(denom, 1.0)

        # --- EMA updates ---
        if not self._initialized:
            self._qi_fast = qi
            self._qi_slow = qi
            self._spread_base = spread_scaled
            self._initialized = True
        else:
            self._qi_fast += _EMA_ALPHA_4 * (qi - self._qi_fast)
            self._qi_slow += _EMA_ALPHA_32 * (qi - self._qi_slow)
            self._spread_base += _EMA_ALPHA_64 * (
                spread_scaled - self._spread_base
            )

        # --- divergence ---
        divergence = self._qi_fast - self._qi_slow

        # --- spread gate: floor 0.1, cap 1.0 ---
        spread_base_safe = max(self._spread_base, 1.0)
        spread_excess = spread_scaled / spread_base_safe - 1.0
        spread_gate = min(max(spread_excess, 0.0) + 0.1, 1.0)

        # --- final signal ---
        raw = divergence * spread_gate
        self._signal = max(-1.0, min(1.0, raw))
        return self._signal

    def reset(self) -> None:
        self._qi_fast = 0.0
        self._qi_slow = 0.0
        self._spread_base = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = ToxicityTimescaleDivergenceAlpha

__all__ = ["ToxicityTimescaleDivergenceAlpha", "ALPHA_CLASS"]
