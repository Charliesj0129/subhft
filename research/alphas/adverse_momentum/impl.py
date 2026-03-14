"""Adverse Selection Momentum Alpha — refs 131, 136.

Signal: signed OFI-return residual from rolling micro-regression.

Paper 131: Cartea, Sanchez-Betancourt (2025) — toxic flow detection via
           OFI-return residual decomposition.
Paper 136: Barzykin, Boyce, Neuman (2024) — hidden alpha process estimation
           under partial information.

Formula:
    delta_mid   = mid_price - prev_mid
    beta        = EMA_32(ofi * delta_mid) / max(EMA_32(ofi^2), eps)
    expected    = beta * ofi
    residual    = delta_mid - expected
    signal      = EMA_8(sign(ofi) * |residual|), clipped to [-2, 2]

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay constants
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)    # ~0.1175
_EMA_ALPHA_32: float = 1.0 - math.exp(-1.0 / 32.0)   # ~0.0308
_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="adverse_momentum",
    hypothesis=(
        "When realized micro-returns consistently exceed the return predicted"
        " by OFI alone, informed traders are present; the signed residual"
        " (direction from OFI, magnitude from unexplained return) captures"
        " the 'hidden alpha process' from Cartea (2025)."
    ),
    formula=(
        "signal = EMA_8(sign(ofi) * |delta_mid - beta * ofi|),"
        " beta = EMA_32(ofi * delta_mid) / max(EMA_32(ofi^2), eps)"
    ),
    paper_refs=("131", "136"),
    data_fields=("mid_price", "ofi_l1_ema8", "spread_scaled"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class AdverseMomentumAlpha:
    """O(1) adverse-selection momentum predictor via OFI-return residual.

    update() accepts either:
      - 3 positional args:  mid_price, ofi_l1_ema8, spread_scaled
      - keyword args:       mid_price=..., ofi_l1_ema8=..., spread_scaled=...
    """

    __slots__ = (
        "_prev_mid",
        "_beta_num_ema",
        "_beta_den_ema",
        "_residual_ema",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._beta_num_ema: float = 0.0
        self._beta_den_ema: float = 0.0
        self._residual_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: C901
        """Ingest one tick and return the updated signal.

        Args:
            mid_price: current mid price (scaled int or float).
            ofi_l1_ema8: EMA-8 smoothed L1 order flow imbalance.
            spread_scaled: bid-ask spread (reserved, unused in v1).
        """
        if args:
            if len(args) < 3:
                raise ValueError(
                    "update() requires 3 positional args"
                    " (mid_price, ofi_l1_ema8, spread_scaled) or keyword args"
                )
            mid_price = float(args[0])
            ofi = float(args[1])
            # spread_scaled = args[2]  — reserved for future use
        else:
            mid_price = float(kwargs.get("mid_price", 0.0))
            ofi = float(kwargs.get("ofi_l1_ema8", 0.0))
            # spread_scaled = kwargs.get("spread_scaled", 0)  — reserved

        # Step 1: compute delta_mid
        if not self._initialized:
            delta_mid = 0.0
            self._prev_mid = mid_price
            self._initialized = True
        else:
            delta_mid = mid_price - self._prev_mid
            self._prev_mid = mid_price

        # Step 2: update rolling beta (OFI -> return regression)
        self._beta_num_ema += _EMA_ALPHA_32 * (
            ofi * delta_mid - self._beta_num_ema
        )
        self._beta_den_ema += _EMA_ALPHA_32 * (
            ofi * ofi - self._beta_den_ema
        )

        # Step 3: compute residual
        beta = self._beta_num_ema / max(self._beta_den_ema, _EPSILON)
        expected = beta * ofi
        residual = delta_mid - expected

        # Step 4: signed residual — direction from OFI, magnitude from residual
        if ofi != 0.0:
            signed_residual = math.copysign(abs(residual), ofi)
        else:
            signed_residual = 0.0

        # Step 5: smooth and clip
        self._residual_ema += _EMA_ALPHA_8 * (
            signed_residual - self._residual_ema
        )
        self._signal = max(-2.0, min(2.0, self._residual_ema))
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._beta_num_ema = 0.0
        self._beta_den_ema = 0.0
        self._residual_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = AdverseMomentumAlpha

__all__ = ["AdverseMomentumAlpha", "ALPHA_CLASS"]
