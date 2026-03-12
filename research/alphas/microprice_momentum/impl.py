"""Microprice Momentum Alpha — microprice delta normalized by spread.

Signal:  MM_t = EMA_8( (microprice_x2_t - microprice_x2_{t-1}) / max(spread_scaled, 1) )

Hypothesis: microprice momentum predicts continued price movement in the
same direction.  Complement to microprice reversion — this is the momentum
(trend-following) signal.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
                 microprice_x2 and spread_scaled are scaled ints from LOBStatsEvent.
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="microprice_momentum",
    hypothesis=(
        "Microprice momentum predicts continued price movement: a rising"
        " microprice signals further upward movement, a falling microprice"
        " signals further downward movement."
    ),
    formula="MM_t = EMA_8( (microprice_x2_t - microprice_x2_{t-1}) / max(spread_scaled, 1) )",
    paper_refs=(),
    data_fields=("microprice_x2", "spread_scaled"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class MicropriceMomentumAlpha:
    """O(1) microprice momentum predictor with EMA smoothing.

    update() accepts either:
      - 2 positional args:  microprice_x2, spread_scaled
      - keyword args:       microprice_x2=..., spread_scaled=...
    """

    __slots__ = ("_prev_micro", "_mom_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._prev_micro: float = 0.0
        self._mom_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update signal with new microprice_x2 and spread_scaled values."""
        # --- resolve microprice_x2 and spread_scaled ---
        if len(args) >= 2:
            microprice_x2 = float(args[0])
            spread_scaled = float(args[1])
        elif len(args) == 1:
            raise ValueError("update() requires 2 positional args (microprice_x2, spread_scaled) or keyword args")
        else:
            microprice_x2 = float(kwargs.get("microprice_x2", 0.0))
            spread_scaled = float(kwargs.get("spread_scaled", 0.0))

        if not self._initialized:
            # First tick: store prev, return 0 (no delta yet).
            self._prev_micro = microprice_x2
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute delta normalized by spread.
        denom = max(spread_scaled, 1.0)
        delta = (microprice_x2 - self._prev_micro) / denom
        self._prev_micro = microprice_x2

        # EMA update.
        self._mom_ema += _EMA_ALPHA_8 * (delta - self._mom_ema)
        self._signal = self._mom_ema
        return self._signal

    def reset(self) -> None:
        self._prev_micro = 0.0
        self._mom_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = MicropriceMomentumAlpha

__all__ = ["MicropriceMomentumAlpha", "ALPHA_CLASS"]
