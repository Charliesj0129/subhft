"""Adverse Flow Asymmetry Alpha — second-moment QI decomposition.

Signal: clip(EMA_8((EMA_16(max(qi,0)^2) - EMA_16(max(-qi,0)^2))
              / max(EMA_16(max(qi,0)^2) + EMA_16(max(-qi,0)^2), eps)), -1, 1)

Detects informed trading via asymmetric variance in queue imbalance.
Papers 129 + 133: "Detecting Toxic Flow" + "Market Simulation under Adverse Selection".

Allocator Law: __slots__ on class; all state is scalar.
Precision Law: output is float (signal score, not price).
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
# EMA decay: window ~ 16 ticks -> alpha = 1 - exp(-1/16) ~ 0.0606
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="adverse_flow_asymmetry",
    hypothesis=(
        "Informed traders preferentially consume one side; the asymmetry of"
        " QI's second moment (squared positive vs squared negative QI) reveals"
        " which side is being informed-traded, even when mean QI is"
        " approximately zero."
    ),
    formula=(
        "signal = clip(EMA_8((EMA_16(max(qi,0)^2) - EMA_16(max(-qi,0)^2))"
        " / max(EMA_16(max(qi,0)^2) + EMA_16(max(-qi,0)^2), eps)), -1, 1)"
    ),
    paper_refs=("129", "133"),
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


class AdverseFlowAsymmetryAlpha:
    """O(1) adverse flow asymmetry signal via second-moment QI decomposition.

    update() accepts either:
      - 2 positional args:  bid_qty, ask_qty
      - keyword args:       bid_qty=..., ask_qty=...
    """

    __slots__ = (
        "_ema_pos_sq",
        "_ema_neg_sq",
        "_asym_ema",
        "_signal",
        "_initialized",
        "_asym_initialized",
    )

    def __init__(self) -> None:
        self._ema_pos_sq: float = 0.0
        self._ema_neg_sq: float = 0.0
        self._asym_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._asym_initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one tick of the adverse flow asymmetry signal.

        Args:
            bid_qty: Best-bid queue size.
            ask_qty: Best-ask queue size.

        Returns:
            The updated signal value clipped to [-1, 1].
        """
        # --- resolve inputs from various call conventions ---
        if len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif args:
            raise ValueError(
                "update() requires 2 positional args"
                " (bid_qty, ask_qty)"
                " or keyword args"
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        # --- queue imbalance ratio ---
        denom = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / max(denom, 1.0)

        # --- squared positive and negative components ---
        pos_sq = max(qi, 0.0) ** 2
        neg_sq = max(-qi, 0.0) ** 2

        # --- EMA-16 of squared components ---
        if not self._initialized:
            self._ema_pos_sq = pos_sq
            self._ema_neg_sq = neg_sq
            self._initialized = True
        else:
            self._ema_pos_sq += _EMA_ALPHA_16 * (pos_sq - self._ema_pos_sq)
            self._ema_neg_sq += _EMA_ALPHA_16 * (neg_sq - self._ema_neg_sq)

        # --- asymmetry ratio ---
        asym_denom = self._ema_pos_sq + self._ema_neg_sq
        asymmetry = (self._ema_pos_sq - self._ema_neg_sq) / max(
            asym_denom, _EPSILON
        )

        # --- EMA-8 smooth the asymmetry ---
        if not self._asym_initialized:
            self._asym_ema = asymmetry
            self._asym_initialized = True
        else:
            self._asym_ema += _EMA_ALPHA_8 * (asymmetry - self._asym_ema)

        # --- clip to [-1, 1] ---
        self._signal = max(-1.0, min(1.0, self._asym_ema))
        return self._signal

    def reset(self) -> None:
        self._ema_pos_sq = 0.0
        self._ema_neg_sq = 0.0
        self._asym_ema = 0.0
        self._signal = 0.0
        self._initialized = False
        self._asym_initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = AdverseFlowAsymmetryAlpha

__all__ = ["AdverseFlowAsymmetryAlpha", "ALPHA_CLASS"]
