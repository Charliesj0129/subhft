"""Session-based dataset partitioning for SubHFT Alpha Mining v2 — Phase 1.

Splits a dataset into Discovery / Selection / Locked-validation / Final-holdout
partitions by *whole session*, assigned contiguously in first-occurrence
(time) order — sessions are never shuffled, so no partition can contain rows
from "the future" relative to an earlier partition. Purge/embargo gaps are
carved out at every partition boundary so no lookback/lookahead window can
straddle two partitions.

Locked-validation and final-holdout are fail-closed: ``get_rows()`` refuses to
return their data until the requesting candidate has been explicitly frozen
via ``freeze_candidate()``. Every access attempt — granted or denied — is
appended to an audit log (``locked_access_log.jsonl``).

This module is deliberately decoupled from ``research/candidate_loop``
(loop_v1, the FROZEN live-registry system) — it takes a generic per-row
session-id array rather than that module's day/symbol conventions, so Mining
v2 carries no dependency on frozen infrastructure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from hft_platform.core import timebase

PARTITION_ORDER: tuple[str, ...] = ("discovery", "selection", "locked_validation", "final_holdout")
LOCKED_PARTITIONS: frozenset[str] = frozenset({"locked_validation", "final_holdout"})
DEFAULT_RATIOS: dict[str, float] = {
    "discovery": 0.50,
    "selection": 0.25,
    "locked_validation": 0.15,
    "final_holdout": 0.10,
}

_FROZEN_FILENAME = "frozen_candidates.jsonl"
_ACCESS_LOG_FILENAME = "locked_access_log.jsonl"


class LockedPartitionAccessError(RuntimeError):
    """Raised when locked-validation/final-holdout rows are requested before candidate freeze."""


class PartitionConfigError(ValueError):
    """Raised for invalid partition configuration: bad ratios, embargo, or non-contiguous sessions."""


@dataclass(frozen=True, slots=True)
class _PartitionSpan:
    name: str
    session_count: int
    raw_row_count: int
    row_indices: np.ndarray  # post-embargo, ascending


class DatasetPartitionManager:
    def __init__(
        self,
        *,
        session_ids: Sequence[Any],
        dataset_fingerprint: str,
        embargo_rows: int,
        manifest_dir: str | Path,
        ratios: Mapping[str, float] | None = None,
        random_seed: int = 42,
    ) -> None:
        if not dataset_fingerprint:
            raise PartitionConfigError("dataset_fingerprint must be non-empty")
        if embargo_rows < 0:
            raise PartitionConfigError(f"embargo_rows must be >= 0, got {embargo_rows}")

        resolved_ratios = dict(ratios) if ratios is not None else dict(DEFAULT_RATIOS)
        missing = set(PARTITION_ORDER) - set(resolved_ratios)
        if missing:
            raise PartitionConfigError(f"ratios missing required partitions: {sorted(missing)}")
        total = sum(resolved_ratios[name] for name in PARTITION_ORDER)
        if abs(total - 1.0) > 1e-6:
            raise PartitionConfigError(f"ratios must sum to 1.0, got {total}")

        self.dataset_fingerprint = dataset_fingerprint
        self.embargo_rows = int(embargo_rows)
        self.ratios: dict[str, float] = {name: float(resolved_ratios[name]) for name in PARTITION_ORDER}
        self.random_seed = int(random_seed)
        self.manifest_dir = Path(manifest_dir)

        sessions_arr = np.asarray(list(session_ids))
        if sessions_arr.size == 0:
            raise PartitionConfigError("session_ids must not be empty")

        self._sessions_arr = sessions_arr
        self._ordered_sessions, self._session_assignment = _assign_sessions(sessions_arr, self.ratios)
        self._spans = _build_spans(sessions_arr, self._session_assignment, self.embargo_rows)
        self._manifest_hash = _compute_manifest_hash(
            dataset_fingerprint=self.dataset_fingerprint,
            ratios=self.ratios,
            embargo_rows=self.embargo_rows,
            random_seed=self.random_seed,
            ordered_sessions=self._ordered_sessions,
            session_assignment=self._session_assignment,
        )

    @property
    def manifest_hash(self) -> str:
        return self._manifest_hash

    def partition_indices(self) -> dict[str, np.ndarray]:
        """Post-embargo row indices per partition, ascending."""
        return {name: span.row_indices.copy() for name, span in self._spans.items()}

    def partition_summary(self) -> dict[str, dict[str, int]]:
        """Per-partition session/row counts (pre- and post-embargo)."""
        return {
            name: {
                "session_count": span.session_count,
                "raw_row_count": span.raw_row_count,
                "post_embargo_row_count": int(span.row_indices.size),
            }
            for name, span in self._spans.items()
        }

    def _manifest_payload(
        self,
        *,
        symbols: Sequence[str],
        feature_schema_version: str,
        target_definition: str,
    ) -> dict[str, Any]:
        return {
            "manifest_hash": self._manifest_hash,
            "dataset_fingerprint": self.dataset_fingerprint,
            "symbols": list(symbols),
            "ratios": self.ratios,
            "embargo_rows": self.embargo_rows,
            "random_seed": self.random_seed,
            "feature_schema_version": feature_schema_version,
            "target_definition": target_definition,
            "created_at_ns": timebase.now_ns(),
            "sessions": [str(s) for s in self._ordered_sessions],
            "session_assignment": {str(s): p for s, p in self._session_assignment.items()},
            "partitions": {
                name: {
                    "session_count": span.session_count,
                    "raw_row_count": span.raw_row_count,
                    "post_embargo_row_count": int(span.row_indices.size),
                }
                for name, span in self._spans.items()
            },
        }

    def write_manifest(
        self,
        path: str | Path,
        *,
        symbols: Sequence[str] = (),
        feature_schema_version: str = "",
        target_definition: str = "",
    ) -> str:
        """Write the immutable ``partition_manifest.json``.

        Idempotent: re-writing to a path that already holds a manifest with the
        same ``manifest_hash`` is a no-op (supports resuming a search run).
        Writing over a manifest with a *different* hash raises, since manifests
        are immutable records.
        """
        out = Path(path)
        payload = self._manifest_payload(
            symbols=symbols,
            feature_schema_version=feature_schema_version,
            target_definition=target_definition,
        )
        if out.exists():
            try:
                existing = json.loads(out.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise PartitionConfigError(f"existing manifest at {out} is unreadable: {exc}") from exc
            if existing.get("manifest_hash") != payload["manifest_hash"]:
                raise PartitionConfigError(
                    f"partition_manifest.json already exists at {out} with a different manifest_hash "
                    "— manifests are immutable; use a new manifest_dir for a different partitioning"
                )
            return str(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(out)

    def get_rows(
        self,
        partition: str,
        features: Mapping[str, np.ndarray],
        *,
        frozen_candidate_id: str | None = None,
    ) -> dict[str, np.ndarray]:
        """Return per-partition row slices. Fail-closed locked-set guard.

        Discovery/Selection: always returns the partition's rows.
        Locked-validation/Final-holdout: raises ``LockedPartitionAccessError``
        unless ``frozen_candidate_id`` was previously frozen via
        ``freeze_candidate()``. Every access attempt is audit-logged.
        """
        if partition not in PARTITION_ORDER:
            raise ValueError(f"unknown partition {partition!r}; must be one of {PARTITION_ORDER}")

        granted = True
        reason = "ok"
        if partition in LOCKED_PARTITIONS:
            if not frozen_candidate_id or not self._is_frozen(frozen_candidate_id):
                granted = False
                reason = "candidate_not_frozen"

        self._log_access(
            partition=partition,
            frozen_candidate_id=frozen_candidate_id or "",
            granted=granted,
            reason=reason,
        )

        if not granted:
            raise LockedPartitionAccessError(
                f"partition {partition!r} is locked; candidate {frozen_candidate_id!r} must be frozen "
                "via freeze_candidate() before this partition can be read"
            )

        idx = self._spans[partition].row_indices
        return {key: np.asarray(arr)[idx] for key, arr in features.items()}

    def freeze_candidate(self, candidate_id: str) -> bool:
        """Idempotently mark *candidate_id* as frozen. Returns True iff newly frozen."""
        if not candidate_id:
            raise ValueError("candidate_id must be non-empty")
        if self._is_frozen(candidate_id):
            return False
        path = self._frozen_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"candidate_id": candidate_id, "frozen_at_ns": timebase.now_ns()}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return True

    def _is_frozen(self, candidate_id: str) -> bool:
        path = self._frozen_path()
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("candidate_id") == candidate_id:
                    return True
        return False

    def _log_access(self, *, partition: str, frozen_candidate_id: str, granted: bool, reason: str) -> None:
        path = self.manifest_dir / _ACCESS_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts_ns": timebase.now_ns(),
            "partition": partition,
            "frozen_candidate_id": frozen_candidate_id,
            "granted": granted,
            "reason": reason,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def _frozen_path(self) -> Path:
        return self.manifest_dir / _FROZEN_FILENAME


def _assign_sessions(
    sessions_arr: np.ndarray,
    ratios: Mapping[str, float],
) -> tuple[list[Any], dict[Any, str]]:
    """Return (unique sessions in first-occurrence order, session -> partition name)."""
    seen: dict[Any, None] = {}
    for sid in sessions_arr.tolist():
        seen.setdefault(sid, None)
    ordered_sessions = list(seen.keys())
    n = len(ordered_sessions)

    counts: dict[str, int] = {}
    allocated = 0
    for i, name in enumerate(PARTITION_ORDER):
        if i == len(PARTITION_ORDER) - 1:
            counts[name] = n - allocated
        else:
            counts[name] = int(round(ratios[name] * n))
            allocated += counts[name]

    assignment: dict[Any, str] = {}
    idx = 0
    for name in PARTITION_ORDER:
        take = counts[name]
        for sid in ordered_sessions[idx : idx + take]:
            assignment[sid] = name
        idx += take

    return ordered_sessions, assignment


def _build_spans(
    sessions_arr: np.ndarray,
    session_assignment: Mapping[Any, str],
    embargo_rows: int,
) -> dict[str, _PartitionSpan]:
    row_partition = np.array([session_assignment[sid] for sid in sessions_arr.tolist()])

    raw_ranges: dict[str, tuple[int, int, int]] = {}  # name -> (min_idx, max_idx, row_count)
    for name in PARTITION_ORDER:
        rows = np.flatnonzero(row_partition == name)
        if rows.size == 0:
            raw_ranges[name] = (-1, -1, 0)
            continue
        min_idx, max_idx = int(rows.min()), int(rows.max())
        if rows.size != (max_idx - min_idx + 1):
            raise PartitionConfigError(
                f"partition {name!r} rows are not contiguous in the input row order — session_ids must be "
                "sorted in time order with each session occupying a contiguous block of rows"
            )
        raw_ranges[name] = (min_idx, max_idx, int(rows.size))

    occupied = [name for name in PARTITION_ORDER if raw_ranges[name][2] > 0]
    trimmed_start: dict[str, int] = {name: raw_ranges[name][0] for name in occupied}
    trimmed_end: dict[str, int] = {name: raw_ranges[name][1] for name in occupied}

    for earlier, later in zip(occupied, occupied[1:], strict=False):
        trimmed_end[earlier] = trimmed_end[earlier] - embargo_rows
        trimmed_start[later] = trimmed_start[later] + embargo_rows

    session_counts: dict[str, int] = {name: 0 for name in PARTITION_ORDER}
    for partition_name in session_assignment.values():
        session_counts[partition_name] += 1

    spans: dict[str, _PartitionSpan] = {}
    for name in PARTITION_ORDER:
        _min_idx, _max_idx, raw_count = raw_ranges[name]
        if raw_count == 0 or name not in trimmed_start:
            spans[name] = _PartitionSpan(
                name=name,
                session_count=session_counts[name],
                raw_row_count=0,
                row_indices=np.asarray([], dtype=np.int64),
            )
            continue
        start = trimmed_start[name]
        end = trimmed_end[name]
        if start > end:
            row_indices = np.asarray([], dtype=np.int64)
        else:
            row_indices = np.arange(start, end + 1, dtype=np.int64)
        spans[name] = _PartitionSpan(
            name=name,
            session_count=session_counts[name],
            raw_row_count=raw_count,
            row_indices=row_indices,
        )
    return spans


def _compute_manifest_hash(
    *,
    dataset_fingerprint: str,
    ratios: Mapping[str, float],
    embargo_rows: int,
    random_seed: int,
    ordered_sessions: Sequence[Any],
    session_assignment: Mapping[Any, str],
) -> str:
    payload = {
        "dataset_fingerprint": dataset_fingerprint,
        "ratios": {name: ratios[name] for name in PARTITION_ORDER},
        "embargo_rows": embargo_rows,
        "random_seed": random_seed,
        "session_assignment": [[str(sid), session_assignment[sid]] for sid in ordered_sessions],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
