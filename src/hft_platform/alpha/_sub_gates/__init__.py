"""Sub-gate registry and implementations for unified Gate C.

This package defines the SubGate protocol (an evaluator for one sub-gate
check) and the in-process registry. Concrete sub-gate implementations
live in sibling modules (common.py, maker.py, taker.py).
"""
from hft_platform.alpha._sub_gates.registry import (
    SubGate,
    SubGateResult,
    clear_registry,
    get_registered_sub_gates,
    register_sub_gate,
)

__all__ = [
    "SubGate",
    "SubGateResult",
    "clear_registry",
    "get_registered_sub_gates",
    "register_sub_gate",
]
