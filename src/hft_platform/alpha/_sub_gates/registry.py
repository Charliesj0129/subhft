"""Sub-gate registry: protocol + in-process registry.

A SubGate is a small evaluator that checks one condition of Gate C
(e.g., Sharpe threshold, maximum drawdown, IC validity). The registry
holds the list of registered sub-gates in insertion order; Gate C's
dispatcher iterates the registry and filters by `applies_to` tags
(strategy_type values: "maker" | "taker").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SubGateResult:
    """Outcome of evaluating one sub-gate.

    ``passed`` is coerced to Python ``bool`` so the result is always
    JSON-serializable without a custom encoder (numpy booleans are not
    handled by stdlib json).
    """

    name: str
    passed: bool
    metrics: dict[str, float] = field(default_factory=dict)
    details: str = ""

    def __post_init__(self) -> None:
        # frozen=True prevents assignment, so use object.__setattr__
        object.__setattr__(self, "passed", bool(self.passed))


@runtime_checkable
class SubGate(Protocol):
    """Protocol for sub-gates in Gate C."""

    name: str
    applies_to: set[str]  # {"maker"}, {"taker"}, or {"maker", "taker"}

    def evaluate(
        self,
        result: Any,
        config: Any,
        thresholds: dict,
    ) -> SubGateResult: ...


# Module-level registry. Mutable by design; use register_sub_gate / clear_registry.
_REGISTRY: list[SubGate] = []


def register_sub_gate(gate: SubGate) -> None:
    """Register a sub-gate. Insertion order is preserved."""
    _REGISTRY.append(gate)


def get_registered_sub_gates() -> list[SubGate]:
    """Return all registered sub-gates in registration order.

    Returns a copy so callers can mutate without affecting the registry.
    """
    return list(_REGISTRY)


def clear_registry() -> None:
    """Clear registry. Primarily for tests."""
    _REGISTRY.clear()
