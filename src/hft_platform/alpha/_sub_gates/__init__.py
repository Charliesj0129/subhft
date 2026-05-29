"""Sub-gate registry and implementations for unified Gate C.

Importing this package auto-registers all built-in sub-gates.
Tests can call ``clear_registry()`` to isolate test state; call
``ensure_builtin_sub_gates_registered()`` to restore defaults.
"""

from hft_platform.alpha._sub_gates.registry import (
    SubGate,
    SubGateResult,
    clear_registry,
    get_registered_sub_gates,
    register_sub_gate,
)


def ensure_builtin_sub_gates_registered() -> None:
    """Ensure all built-in sub-gates are registered (idempotent by name).

    Safe to call multiple times — existing gates are preserved. Only
    missing gates (by ``name`` attribute) are added. Preserves insertion
    order of existing gates.
    """
    from hft_platform.alpha._sub_gates.common import (
        MaxDrawdownGate,
        SharpeThresholdGate,
        WinningDayPctGate,
    )
    from hft_platform.alpha._sub_gates.cost_uncertainty import CostUncertaintyGate
    from hft_platform.alpha._sub_gates.day_bootstrap_ci import DayLevelBootstrapCIGate
    from hft_platform.alpha._sub_gates.deflated_sharpe_maker import (
        DeflatedSharpeForMakerGate,
    )
    from hft_platform.alpha._sub_gates.edge_per_round_trip import (
        EdgePerRoundTripGate,
    )
    from hft_platform.alpha._sub_gates.inventory_mtm import InventoryMtMGate
    from hft_platform.alpha._sub_gates.loo_day_sensitivity import LOODaySensitivityGate
    from hft_platform.alpha._sub_gates.maker import (
        FillQualityGate,
        FillRateValidationGate,
    )
    from hft_platform.alpha._sub_gates.min_sample_size import MinSampleSizeGate
    from hft_platform.alpha._sub_gates.monthly_distribution import (
        MonthlyDistributionGate,
    )
    from hft_platform.alpha._sub_gates.outlier_trade_removal import (
        OutlierTradeRemovalGate,
    )
    from hft_platform.alpha._sub_gates.replay_parity import ReplayParityGate
    from hft_platform.alpha._sub_gates.single_day_dominance import (
        SingleDayDominanceGate,
    )
    from hft_platform.alpha._sub_gates.stationary_block_bootstrap import (
        StationaryBlockBootstrapGate,
    )
    from hft_platform.alpha._sub_gates.taker import ICEvaluationGate
    from hft_platform.alpha._sub_gates.trade_concentration import (
        TradeConcentrationGate,
    )

    existing_names = {g.name for g in get_registered_sub_gates()}
    candidates: list[SubGate] = [
        # Existing
        SharpeThresholdGate(),
        MaxDrawdownGate(),
        WinningDayPctGate(),
        FillQualityGate(),
        FillRateValidationGate(),
        ICEvaluationGate(),
        # New (Slice A)
        MinSampleSizeGate(),
        SingleDayDominanceGate(),
        LOODaySensitivityGate(),
        OutlierTradeRemovalGate(),
        DayLevelBootstrapCIGate(),
        StationaryBlockBootstrapGate(),
        DeflatedSharpeForMakerGate(),
        # New (Slice C)
        ReplayParityGate(),
        # New (Slice B)
        InventoryMtMGate(),
        CostUncertaintyGate(),
        # Monthly-distribution credibility gate (goal §6)
        MonthlyDistributionGate(),
        # Per-round-trip net edge floor (goal §5: > 10 pts/trade)
        EdgePerRoundTripGate(),
        # Trade-level concentration (goal §5: loss-distribution + dominance)
        TradeConcentrationGate(),
    ]
    for gate in candidates:
        if gate.name not in existing_names:
            register_sub_gate(gate)


# Register built-in gates once at import time.
ensure_builtin_sub_gates_registered()


__all__ = [
    "SubGate",
    "SubGateResult",
    "clear_registry",
    "ensure_builtin_sub_gates_registered",
    "get_registered_sub_gates",
    "register_sub_gate",
]
