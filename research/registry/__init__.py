"""Research registry compatibility exports.

Keep package import closure lightweight; concrete registry services are loaded
only when a caller requests their historical package-level names.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from research.registry.alpha_registry import AlphaRegistry
    from research.registry.correlation_tracker import CorrelationTracker
    from research.registry.pool_optimizer import AlphaPoolOptimizer
    from research.registry.schemas import AlphaManifest, AlphaProtocol, AlphaStatus, AlphaTier, Scorecard
    from research.registry.scorecard import compute_scorecard, load_scorecard, save_scorecard

__all__ = [
    "AlphaPoolOptimizer",
    "AlphaManifest",
    "AlphaProtocol",
    "AlphaRegistry",
    "AlphaStatus",
    "AlphaTier",
    "CorrelationTracker",
    "Scorecard",
    "compute_scorecard",
    "load_scorecard",
    "save_scorecard",
]

_EXPORTS = {
    "AlphaManifest": ("research.registry.schemas", "AlphaManifest"),
    "AlphaPoolOptimizer": ("research.registry.pool_optimizer", "AlphaPoolOptimizer"),
    "AlphaProtocol": ("research.registry.schemas", "AlphaProtocol"),
    "AlphaRegistry": ("research.registry.alpha_registry", "AlphaRegistry"),
    "AlphaStatus": ("research.registry.schemas", "AlphaStatus"),
    "AlphaTier": ("research.registry.schemas", "AlphaTier"),
    "CorrelationTracker": ("research.registry.correlation_tracker", "CorrelationTracker"),
    "Scorecard": ("research.registry.schemas", "Scorecard"),
    "compute_scorecard": ("research.registry.scorecard", "compute_scorecard"),
    "load_scorecard": ("research.registry.scorecard", "load_scorecard"),
    "save_scorecard": ("research.registry.scorecard", "save_scorecard"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
