"""Imbalance Divergence Alpha — L1 vs depth imbalance disagreement.

Signal:  EMA_8((l1_imbalance_ppm - depth_imbalance_ppm) / 1_000_000)

Hypothesis: When L1 imbalance disagrees with deeper book imbalance,
informed traders concentrate at best price. L1 >> depth signals
short-term momentum; L1 << depth signals deeper book knows better.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : inputs are scaled int (ppm); output is float (signal score).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
_PPM_SCALE: int = 1_000_000

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="imbalance_divergence",
    hypothesis=(
        "When L1 imbalance disagrees with deeper book imbalance, informed"
        " traders concentrate at best price. L1 >> depth signals short-term"
        " momentum; L1 << depth signals deeper book knows better."
    ),
    formula="EMA_8((l1_imbalance_ppm - depth_imbalance_ppm) / 1_000_000)",
    paper_refs=(),
    data_fields=("l1_imbalance_ppm", "depth_imbalance_ppm"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class ImbalanceDivergenceAlpha:
    """O(1) imbalance divergence predictor with EMA smoothing.

    update() accepts either:
      - 2 positional args:  l1_imbalance_ppm, depth_imbalance_ppm
      - keyword args:       l1_imbalance_ppm=..., depth_imbalance_ppm=...
    """

    __slots__ = ("_div_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._div_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute divergence signal from L1 and depth imbalance (ppm)."""
        # --- resolve l1_imbalance_ppm and depth_imbalance_ppm ---
        if len(args) >= 2:
            l1_ppm = float(args[0])
            depth_ppm = float(args[1])
        elif len(args) == 1:
            raise ValueError(
                "update() requires 2 positional args (l1_imbalance_ppm, depth_imbalance_ppm) or keyword args"
            )
        else:
            l1_ppm = float(kwargs.get("l1_imbalance_ppm", 0.0))
            depth_ppm = float(kwargs.get("depth_imbalance_ppm", 0.0))

        divergence = (l1_ppm - depth_ppm) / _PPM_SCALE

        if not self._initialized:
            self._div_ema = divergence
            self._initialized = True
        else:
            self._div_ema += _EMA_ALPHA_8 * (divergence - self._div_ema)

        self._signal = self._div_ema
        return self._signal

    def reset(self) -> None:
        self._div_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = ImbalanceDivergenceAlpha

__all__ = ["ImbalanceDivergenceAlpha", "ALPHA_CLASS"]
