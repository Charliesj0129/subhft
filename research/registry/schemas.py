"""Compatibility exports for the canonical alpha-governance contracts.

New platform code must import from :mod:`hft_platform.contracts.alpha`.
This module remains as a stable resolver for research callers and historical
serialized artifacts that reference ``research.registry.schemas``.
"""

from hft_platform.contracts.alpha import (
    DEFAULT_LATENCY_PROFILE,
    EDGE_METRIC_SEMANTICS_SCHEMA,
    EDGE_METRIC_SOURCE_GATE,
    EDGE_METRIC_SUPPORTING_GATES,
    VALID_ROLES,
    VALID_SKILLS,
    AlphaManifest,
    AlphaProtocol,
    AlphaStatus,
    AlphaTier,
    BatchAlphaProtocol,
    Scorecard,
    edge_metric_semantics,
)

__all__ = (
    "AlphaManifest",
    "AlphaProtocol",
    "AlphaStatus",
    "AlphaTier",
    "BatchAlphaProtocol",
    "DEFAULT_LATENCY_PROFILE",
    "EDGE_METRIC_SEMANTICS_SCHEMA",
    "EDGE_METRIC_SOURCE_GATE",
    "EDGE_METRIC_SUPPORTING_GATES",
    "Scorecard",
    "VALID_ROLES",
    "VALID_SKILLS",
    "edge_metric_semantics",
)
