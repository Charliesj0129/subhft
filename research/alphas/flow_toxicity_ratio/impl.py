"""Flow Toxicity Ratio Alpha — unsigned OFI magnitude / L1 liquidity.

Signal:  FTR_t = EMA_16( |ofi_l1_raw| / max(l1_bid_qty + l1_ask_qty, 1) )
Hypothesis: Ratio of OFI magnitude to total L1 liquidity measures flow
toxicity.  High ratio = informed traders consuming disproportionate
liquidity.  Unlike directional OFI alphas this is UNSIGNED (magnitude
only, no direction) — it measures toxicity *level*, not which side.

Allocator Law  : __slots__ on class; all state is scalar.
Precision Law  : output is float (signal score, not price — no Decimal needed).
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# EMA decay: window ≈ 16 ticks → α = 1 − exp(−1/16) ≈ 0.0606
_EMA_ALPHA_16: float = 1.0 - math.exp(-1.0 / 16.0)

# Cached manifest (Allocator Law: no per-call heap allocation).
_MANIFEST = AlphaManifest(
    alpha_id="flow_toxicity_ratio",
    hypothesis=(
        "Ratio of absolute OFI to total L1 queue depth measures flow"
        " toxicity: high ratio indicates informed traders consuming"
        " disproportionate liquidity regardless of direction."
    ),
    formula="FTR_t = EMA_16( |ofi_l1_raw| / max(l1_bid_qty + l1_ask_qty, 1) )",
    paper_refs=(),
    data_fields=("ofi_l1_raw", "l1_bid_qty", "l1_ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class FlowToxicityRatioAlpha:
    """O(1) unsigned flow-toxicity ratio with EMA-16 smoothing.

    update() accepts either:
      - 3 positional args:  ofi_l1_raw, l1_bid_qty, l1_ask_qty
      - keyword args:       ofi_l1_raw=..., l1_bid_qty=..., l1_ask_qty=...
    """

    __slots__ = ("_toxicity_ema", "_signal", "_initialized")

    def __init__(self) -> None:
        self._toxicity_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: float) -> float:
        """Compute flow toxicity ratio and update EMA."""
        if len(args) >= 3:
            ofi_l1_raw = float(args[0])
            l1_bid_qty = float(args[1])
            l1_ask_qty = float(args[2])
        elif args:
            raise ValueError("update() requires 3 positional args (ofi_l1_raw, l1_bid_qty, l1_ask_qty) or keyword args")
        else:
            ofi_l1_raw = float(kwargs.get("ofi_l1_raw", 0.0))
            l1_bid_qty = float(kwargs.get("l1_bid_qty", 0.0))
            l1_ask_qty = float(kwargs.get("l1_ask_qty", 0.0))

        denom = max(l1_bid_qty + l1_ask_qty, 1.0)
        raw_ratio = abs(ofi_l1_raw) / denom

        if not self._initialized:
            self._toxicity_ema = raw_ratio
            self._initialized = True
        else:
            self._toxicity_ema += _EMA_ALPHA_16 * (raw_ratio - self._toxicity_ema)

        self._signal = self._toxicity_ema
        return self._signal

    def reset(self) -> None:
        self._toxicity_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = FlowToxicityRatioAlpha

__all__ = ["FlowToxicityRatioAlpha", "ALPHA_CLASS"]
