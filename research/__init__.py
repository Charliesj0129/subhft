"""Research factor factory -- public API.

Public compatibility exports are resolved lazily so importing an unrelated
``research.*`` module does not eagerly load the legacy registry implementation.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from research.registry.alpha_registry import AlphaRegistry
    from research.registry.schemas import (
        AlphaManifest,
        AlphaProtocol,
        AlphaStatus,
        AlphaTier,
        Scorecard,
    )

__all__ = [
    "AlphaManifest",
    "AlphaProtocol",
    "AlphaRegistry",
    "AlphaStatus",
    "AlphaTier",
    "Scorecard",
]

_EXPORTS = {
    "AlphaManifest": ("research.registry.schemas", "AlphaManifest"),
    "AlphaProtocol": ("research.registry.schemas", "AlphaProtocol"),
    "AlphaRegistry": ("research.registry.alpha_registry", "AlphaRegistry"),
    "AlphaStatus": ("research.registry.schemas", "AlphaStatus"),
    "AlphaTier": ("research.registry.schemas", "AlphaTier"),
    "Scorecard": ("research.registry.schemas", "Scorecard"),
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
