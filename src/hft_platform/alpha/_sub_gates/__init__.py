"""Sub-gate registry and implementations for unified Gate C.

Importing this package auto-registers all built-in sub-gates.
Tests can call `clear_registry()` and re-import to reset.
"""
from hft_platform.alpha._sub_gates.registry import (
    SubGate,
    SubGateResult,
    clear_registry,
    get_registered_sub_gates,
    register_sub_gate,
)


def _register_builtin_sub_gates() -> None:
    """Register all shipped sub-gates. Called once at import time."""
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

    # Order: common first, then strategy-specific
    register_sub_gate(SharpeThresholdGate())
    register_sub_gate(MaxDrawdownGate())
    register_sub_gate(WinningDayPctGate())
    register_sub_gate(FillQualityGate())
    register_sub_gate(FillRateValidationGate())
    register_sub_gate(ICEvaluationGate())


_register_builtin_sub_gates()


__all__ = [
    "SubGate",
    "SubGateResult",
    "clear_registry",
    "get_registered_sub_gates",
    "register_sub_gate",
]
