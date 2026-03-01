"""spread_pressure alpha: spread widening vs EMA-8 baseline × depth imbalance direction.

Research implementation (Gate A–E pipeline).

Hypothesis:
    When the current bid-ask spread widens above its EMA-8 baseline AND the depth
    imbalance confirms directional pressure, the combination predicts short-term
    adverse selection. A widening spread signals the market maker is widening for
    adverse selection; the depth imbalance provides the direction.

Formula:
    spread_diff = spread_ema8_scaled - spread_scaled   (positive = tighter than EMA)
    signal = spread_diff × sign(depth_imbalance_ema8_ppm) / max(|spread_ema8_scaled|, 1)

Feature Engine indices (lob_shared_v1, schema_version=1):
    Index  3: spread_scaled            (stateless, scale=10000)
    Index 14: spread_ema8_scaled       (rolling, warmup=2)
    Index 15: depth_imbalance_ema8_ppm (rolling, warmup=2)

HFT Laws compliance:
    - Precision Law: spread_scaled/spread_ema8_scaled are int ×10000;
      division gives float for ranking only — never used as a price.
    - Allocator Law: O(1), no heap allocation per tick; __slots__ used.
    - Async Law: Stateless per-tick computation, no blocking IO.
"""
from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


_MANIFEST = AlphaManifest(
    alpha_id="spread_pressure",
    hypothesis=(
        "Spread widening vs EMA-8 baseline combined with depth imbalance direction "
        "predicts short-term adverse selection. A widening spread signals the market "
        "maker is widening for adverse selection; depth imbalance provides direction."
    ),
    formula=(
        "spread_diff = spread_ema8_scaled - spread_scaled; "
        "signal = spread_diff × sign(depth_imbalance_ema8_ppm) / max(|spread_ema8_scaled|, 1)"
    ),
    paper_refs=(),
    data_fields=("spread_scaled", "spread_ema8_scaled", "depth_imbalance_ema8_ppm"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    # Latency realism governance (CLAUDE.md constitution requirement).
    # Profile: Shioaji sim API P95 RTT — submit≈36ms, modify≈43ms, cancel≈47ms.
    # Source: docs/architecture/latency-baseline-shioaji-sim-vs-system.md
    latency_profile="shioaji_sim_p95_v2026-02-28",
    # SOP governance: roles and skills applied during research.
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class SpreadPressureAlpha:
    """AlphaProtocol-conforming spread pressure signal.

    Tier: TIER_2 (EMA-based, no LOB arrays required)
    Complexity: O(1)
    """

    __slots__ = ("_signal",)

    def __init__(self) -> None:
        self._signal: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        # Module-level constant — no allocation on every call (Allocator Law).
        return _MANIFEST

    def update(
        self,
        spread_scaled: int,
        spread_ema8_scaled: int,
        depth_imbalance_ema8_ppm: int,
    ) -> float:
        """Compute spread pressure signal from a single tick's feature values.

        Parameters
        ----------
        spread_scaled:
            Current bid-ask spread, scaled ×10000 (int).
        spread_ema8_scaled:
            EMA(8) of spread_scaled (int ×10000).
        depth_imbalance_ema8_ppm:
            EMA(8) of depth imbalance in PPM (int). Positive = bid-heavy.

        Returns
        -------
        float
            Dimensionless signal in approximately (-1, +1) range.
            For ranking only — must not be used as a price.
        """
        # Coerce to int at the boundary (Precision Law: inputs are scaled int ×10000;
        # float inputs from numpy array elements would silently violate the contract).
        s = int(spread_scaled)
        ema = int(spread_ema8_scaled)
        imb = int(depth_imbalance_ema8_ppm)
        diff = ema - s
        if imb > 0:
            sign_imb = 1
        elif imb < 0:
            sign_imb = -1
        else:
            sign_imb = 0
        denom = max(abs(ema), 1)
        self._signal = float(diff * sign_imb) / float(denom)
        return self._signal

    def reset(self) -> None:
        """Reset cached signal to 0.0 (e.g. between symbols or sessions)."""
        self._signal = 0.0

    def get_signal(self) -> float:
        """Return last computed signal without recomputing."""
        return self._signal


ALPHA_CLASS = SpreadPressureAlpha

__all__ = ["SpreadPressureAlpha", "ALPHA_CLASS"]
