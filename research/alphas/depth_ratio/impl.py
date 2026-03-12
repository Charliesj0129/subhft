"""Depth Ratio Alpha — log book asymmetry signal.

Signal:  DR_t = EMA_8( log( max(bid_depth, 1) / max(ask_depth, 1) ) )
Smoothed via exponential moving average (alpha_ema ~ 1 - exp(-1/8)).

A positive DR -> bid-side depth dominates -> near-term upward mid-price pressure.
A negative DR -> ask-side depth dominates -> near-term downward pressure.

Log scale handles extreme depth distributions and is more stable than linear
imbalance, compressing large ratios while preserving sign and monotonicity.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price -- no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ~ 8 ticks -> alpha = 1 - exp(-1/8) ~ 0.1175
_EMA_ALPHA_8: float = 1.0 - math.exp(-1.0 / 8.0)
# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="depth_ratio",
    hypothesis=(
        "Log depth ratio is a smoother representation of book asymmetry than"
        " linear imbalance. Log scale handles extreme depth distributions and"
        " is more stable."
    ),
    formula="DR_t = EMA_8( log( max(bid_depth, 1) / max(ask_depth, 1) ) )",
    paper_refs=(),
    data_fields=("bid_depth", "ask_depth"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class DepthRatioAlpha:
    """O(1) log-depth-ratio predictor with EMA smoothing.

    update() accepts either:
      - 2 positional args:  bid_depth, ask_depth
      - keyword args:       bid_depth=..., ask_depth=...
    """

    __slots__ = ("_log_ratio_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._log_ratio_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute log depth ratio and update EMA."""
        # --- resolve bid_depth and ask_depth from call conventions ---
        if len(args) >= 2:
            bid_depth = float(args[0])
            ask_depth = float(args[1])
        elif len(args) == 1:
            raise ValueError("update() requires 2 positional args (bid_depth, ask_depth) or keyword args")
        else:
            bid_depth = float(kwargs.get("bid_depth", 0.0))
            ask_depth = float(kwargs.get("ask_depth", 0.0))

        # Guard against zero/negative depth: max(x, 1) ensures log is defined
        safe_bid = max(bid_depth, 1.0)
        safe_ask = max(ask_depth, 1.0)
        raw_log_ratio = math.log(safe_bid / safe_ask)

        if not self._initialized:
            self._log_ratio_ema = raw_log_ratio
            self._initialized = True
        else:
            self._log_ratio_ema += _EMA_ALPHA_8 * (raw_log_ratio - self._log_ratio_ema)

        self._signal = self._log_ratio_ema
        return self._signal

    def reset(self) -> None:
        self._log_ratio_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = DepthRatioAlpha

__all__ = ["DepthRatioAlpha", "ALPHA_CLASS"]
