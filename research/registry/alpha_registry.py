from __future__ import annotations

from typing import Any, Iterable, Mapping

from hft_platform.alpha.discovery import AlphaDiscoveryRegistry
from hft_platform.alpha.discovery import _to_module_name as _canonical_to_module_name
from research.registry import schemas as _schemas
from research.registry.correlation_tracker import CorrelationTracker

AlphaManifest = _schemas.AlphaManifest
AlphaProtocol = _schemas.AlphaProtocol
AlphaStatus = _schemas.AlphaStatus
AlphaTier = _schemas.AlphaTier
_to_module_name = _canonical_to_module_name


class AlphaRegistry(AlphaDiscoveryRegistry):
    """Research compatibility registry with correlation analysis."""

    def compute_correlation_matrix(self, signals: Mapping[str, Iterable[float]]) -> dict[str, Any]:
        tracker = CorrelationTracker()
        return tracker.compute_matrix(signals)
