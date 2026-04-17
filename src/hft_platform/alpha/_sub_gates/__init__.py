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
    from hft_platform.alpha._sub_gates.maker import (
        FillQualityGate,
        FillRateValidationGate,
    )
    from hft_platform.alpha._sub_gates.taker import ICEvaluationGate

    existing_names = {g.name for g in get_registered_sub_gates()}
    candidates = [
        SharpeThresholdGate(),
        MaxDrawdownGate(),
        WinningDayPctGate(),
        FillQualityGate(),
        FillRateValidationGate(),
        ICEvaluationGate(),
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
