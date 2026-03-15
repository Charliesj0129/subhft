"""metaorder_bocpd alpha — stub implementation."""
from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

_MANIFEST = AlphaManifest(
    alpha_id="metaorder_bocpd",
    hypothesis="Stub — pending research.",
    formula="N/A",
    paper_refs=(),
    data_fields=("price", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
)


class MetaorderBocpdAlpha:
    """Stub alpha for metaorder_bocpd."""

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


ALPHA_CLASS = MetaorderBocpdAlpha

__all__ = ["MetaorderBocpdAlpha", "ALPHA_CLASS"]
