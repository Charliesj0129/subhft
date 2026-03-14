"""Toxicity Multiscale Alpha — ref 129 (Cartea, Duran-Martin, Sanchez-Betancourt 2023).

Signal:  Multi-timescale interaction of volatility, queue imbalance, and spread
         deviation captures informed trading more completely than any single-scale
         measure.

Formula:
    QI = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps)
    volatility = EMA_16(|delta_mid|)
    spread_dev = spread_scaled / max(EMA_64(spread_scaled), 1.0)
    raw = volatility * |QI| * spread_dev
    signal = sign(QI) * EMA_8(raw), clipped to [-2, 2]

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA constants: alpha = 1 - exp(-1/window)
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)    # ~0.1175
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)   # ~0.0606
_EMA_ALPHA_64: float = 1.0 - math.exp(-1.0 / 64.0)   # ~0.0155
_EPSILON: float = 1e-8  # guards against division by zero

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="toxicity_multiscale",
    hypothesis=(
        "Multi-timescale interaction of volatility, queue imbalance, and spread"
        " deviation captures informed trading more completely than any single-scale"
        " measure; the multiplicative composite isolates periods where all three"
        " toxicity indicators align."
    ),
    formula=(
        "signal = sign(QI) * EMA_8(EMA_16(|dP|) * |QI| * spread_scaled / EMA_64(spread_scaled)),"
        " clipped [-2, 2]"
    ),
    paper_refs=("129",),
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
    """O(1) multi-timescale toxicity composite with EMA smoothing.

    update() accepts either:
      - 4 positional args: bid_qty, ask_qty, spread_scaled, mid_price
      - keyword args:      bid_qty=..., ask_qty=..., spread_scaled=..., mid_price=...
    """

    __slots__ = (
        "_prev_mid",
        "_vol16",
        "_spread_base64",
        "_composite_ema8",
        "_signal",
        "_initialized",
        "_vol_initialized",
    )

    def __init__(self) -> None:
        self._prev_mid: float = 0.0
        self._vol16: float = 0.0
        self._spread_base64: float = 0.0
        self._composite_ema8: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._vol_initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute one tick of the toxicity multiscale signal.

        Returns the current signal value (float in [-2, 2]).
        """
        # --- resolve inputs from various call conventions ---
        if len(args) == 4:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
            spread_scaled = float(args[2])
            mid_price = float(args[3])
        elif args:
            raise ValueError(
                "update() requires 4 positional args"
                " (bid_qty, ask_qty, spread_scaled, mid_price) or keyword args"
            )
        else:
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))
            mid_price = float(kwargs.get("mid_price", 0.0))

        # 1. Queue imbalance
        denom = bid_qty + ask_qty
        qi = (bid_qty - ask_qty) / (denom + _EPSILON)

        # 2. Price volatility (EMA-16 of |delta_mid|)
        if not self._initialized:
            delta_mid = 0.0
            self._prev_mid = mid_price
            self._initialized = True
        else:
            delta_mid = mid_price - self._prev_mid
            self._prev_mid = mid_price

        abs_delta = abs(delta_mid)
        if not self._vol_initialized:
            self._vol16 = abs_delta
            self._spread_base64 = spread_scaled
            self._vol_initialized = True
        else:
            self._vol16 += _EMA_ALPHA_16 * (abs_delta - self._vol16)
            self._spread_base64 += _EMA_ALPHA_64 * (spread_scaled - self._spread_base64)

        # 3. Spread deviation
        spread_dev = spread_scaled / max(self._spread_base64, 1.0)

        # 4. Raw composite
        raw = self._vol16 * abs(qi) * spread_dev

        # 5. Composite EMA-8
        self._composite_ema8 += _EMA_ALPHA_8 * (raw - self._composite_ema8)

        # 6. Directional signal, clipped
        self._signal = max(-2.0, min(2.0, math.copysign(self._composite_ema8, qi)))
        return self._signal

    def reset(self) -> None:
        self._prev_mid = 0.0
        self._vol16 = 0.0
        self._spread_base64 = 0.0
        self._composite_ema8 = 0.0
        self._signal = 0.0
        self._initialized = False
        self._vol_initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = ToxicityMultiscaleAlpha

__all__ = ["ToxicityMultiscaleAlpha", "ALPHA_CLASS"]
