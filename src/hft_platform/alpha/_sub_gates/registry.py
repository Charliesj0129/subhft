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

from hft_platform.alpha.outcomes import Outcome, resolve_outcome


@dataclass(frozen=True)
class SubGateResult:
    """Outcome of evaluating one sub-gate.

    ``passed`` is coerced to Python ``bool`` so the result is always
    JSON-serializable without a custom encoder (numpy booleans are not
    handled by stdlib json).

    ``outcome`` says *why* a gate did not pass (see ``alpha.outcomes``). When
    omitted it is derived from ``passed``; a contradiction raises ``ValueError``.
    """

    name: str
    passed: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    details: str = ""
    outcome: Outcome | None = None

    def __post_init__(self) -> None:
        # frozen=True prevents assignment, so use object.__setattr__
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "outcome", resolve_outcome(self.passed, self.outcome))


class UnavailableDailyPnl:
    """Stands in for ``daily_pnl`` when a run has no dated per-day rows.

    Any read raises, so every day-axis gate reports ERROR instead of measuring
    a series that is not one value per trading day.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def _unavailable(self, *_: Any) -> Any:
        raise ValueError(f"daily_pnl unavailable: {self.reason}")

    __iter__ = __len__ = __bool__ = __getitem__ = _unavailable


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
