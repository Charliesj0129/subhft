"""CE3-04: ReplayContract — precondition validation for WAL replay.

Defines the contract that a WALLoaderService instance must satisfy before
starting file processing. Violations are returned as a list of human-readable
strings (not exceptions) so callers can decide to warn or abort.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass
class ReplayContract:
    """Configuration contract for a WAL replay session."""

    file_ordering: Literal["strict_ns", "best_effort"] = "best_effort"
    dedup_enabled: bool = False
    manifest_enabled: bool = True
    require_archive_on_success: bool = True


def validate_replay_preconditions(loader: Any) -> list[str]:
    """Validate a WALLoaderService instance against its implied ReplayContract.

    Returns a list of violation strings; empty list = all OK.
    """
    violations: list[str] = []

    # Build implied contract from loader settings
    strict_order = getattr(loader, "_strict_order", False)
    dedup_enabled = getattr(loader, "_dedup_enabled", False)
    manifest_enabled = getattr(loader, "_manifest_enabled", True)
    archive_dir = getattr(loader, "archive_dir", None)

    # strict_ns requires manifest (to avoid reprocessing)
    if strict_order and not manifest_enabled:
        violations.append(
            "strict_ns file ordering requires manifest (HFT_WAL_USE_MANIFEST=1)"
        )

    # dedup requires ClickHouse client (writes to hft._wal_dedup)
    if dedup_enabled:
        ch_client = getattr(loader, "ch_client", None)
        if ch_client is None:
            violations.append(
                "dedup_enabled requires an active ClickHouse client (ch_client is None)"
            )

    # archive_on_success requires archive_dir to be set
    if archive_dir is None:
        violations.append("archive_dir must be set for require_archive_on_success")

    return violations
