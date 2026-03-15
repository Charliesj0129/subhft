"""Volume Surprise Alpha — directional volume surprise signal.

Signal:  VS_t = (volume / EMA_32(volume) - 1) * sign(bid_qty - ask_qty)

Hypothesis: Volume surprise signals information arrival; abnormally high
volume precedes directional moves.  Combined with bid/ask imbalance to
determine direction.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 32 ticks -> alpha = 1 - exp(-1/32) ~ 0.0308
_EMA_ALPHA_32: float = 1.0 - math.exp(-1.0 / 32.0)

_EPSILON: float = 1e-8

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="volume_surprise",
    hypothesis=(
        "Volume surprise signals information arrival; combined with"
        " imbalance gives direction"
    ),
    formula="VS_t = (volume / EMA_32(volume) - 1) * sign(bid_qty - ask_qty)",
    paper_refs=(),
    data_fields=("volume", "bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner",),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class VolumeSurpriseAlpha:
    """O(1) directional volume surprise predictor with EMA smoothing.

    update() accepts either:
      - 3 positional args:  volume, bid_qty, ask_qty
      - keyword args:       volume=..., bid_qty=..., ask_qty=...
    """

    __slots__ = ("_vol_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._vol_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:  # noqa: ANN002
        """Update signal with new volume, bid_qty, ask_qty values."""
        # --- resolve inputs ---
        if len(args) >= 3:
            volume = float(args[0])
            bid_qty = float(args[1])
            ask_qty = float(args[2])
        elif len(args) in (1, 2):
            raise ValueError(
                "update() requires 3 positional args (volume, bid_qty, ask_qty)"
                " or keyword args"
            )
        else:
            volume = float(kwargs.get("volume", 0.0))
            bid_qty = float(kwargs.get("bid_qty", 0.0))
            ask_qty = float(kwargs.get("ask_qty", 0.0))

        if not self._initialized:
            # First tick: seed EMA, return 0 (no surprise reference yet).
            self._vol_ema = max(volume, _EPSILON)
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Compute surprise ratio.
        surprise = volume / max(self._vol_ema, _EPSILON) - 1.0

        # Direction from bid/ask imbalance.
        if bid_qty > ask_qty:
            direction = 1.0
        elif ask_qty > bid_qty:
            direction = -1.0
        else:
            direction = 0.0

        self._signal = surprise * direction

        # Update EMA *after* computing surprise (no lookahead).
        self._vol_ema += _EMA_ALPHA_32 * (volume - self._vol_ema)

        return self._signal

    def reset(self) -> None:
        self._vol_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = VolumeSurpriseAlpha

__all__ = ["VolumeSurpriseAlpha", "ALPHA_CLASS"]
