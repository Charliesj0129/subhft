"""vpin_bvc alpha — stub implementation."""
from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_MANIFEST = AlphaManifest(
    alpha_id="vpin_bvc",
    hypothesis="Stub — pending research.",
    formula="N/A",
    paper_refs=(),
    data_fields=("price", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_3,
    rust_module=None,
)


class VpinBvcAlpha:
    """Stub alpha for vpin_bvc."""

    __slots__ = ("_signal",)

    def __init__(self) -> None:
        self._signal: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args, **kwargs) -> float:  # noqa: ARG002
        return self._signal

    def value(self) -> float:
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0


ALPHA_CLASS = VpinBvcAlpha

__all__ = ["VpinBvcAlpha", "ALPHA_CLASS"]
