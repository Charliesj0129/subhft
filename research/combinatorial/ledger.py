"""Trial ledger for SubHFT Alpha Mining v2 — Phase 1.

Append-only JSONL record of every search trial, including compile failures
and duplicates, so the multiple-testing correction (Deflated Sharpe, existing
``research/combinatorial`` logic) can be given an honest trial count instead
of only counting the trials that happened to pass.

Mirrors ``src/hft_platform/alpha/kill_ledger.py``'s idempotent-JSONL-sink
pattern (dedupe key stored per row, warm-once-per-instance cache, env-var path
override for test isolation) but is rewritten locally — this ledger is JSONL
only, no ClickHouse sink, since there is no evidence Mining v2 needs one yet.

Trial identity: ``sha256(semantic_identity : dataset_fingerprint :
partition_manifest_hash : algorithm)``, where ``semantic_identity`` is
``canonical_ast.canonical_hash(expression)`` (Phase 2) when the expression is
grammar-valid, falling back to a plain normalized-text hash when it isn't
(canonicalization is only defined over syntactically-valid expressions, and
malformed expressions still need a stable identity for
``record_compile_failure``). The canonical form collapses commutative
reordering, ``BinOp``/``Call`` equivalence, and numeric-literal formatting —
tightening Phase 1's original purely-textual identity, which only ever
overcounted trials feeding the multiple-testing correction, never
undercounted them. Existing ``_trial_ledger.jsonl`` rows recorded under the
old textual identity are left as-is (append-only; no migration needed since
the old identity was a strict superset of "textually different").
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from hft_platform.core import timebase
from research.combinatorial.canonical_ast import canonical_hash

_DEFAULT_JSONL_PATH = Path("research/combinatorial/results/_trial_ledger.jsonl")


def _default_jsonl_path() -> Path:
    """Resolve the jsonl path; ``HFT_ALPHA_MINE_TRIAL_LEDGER_PATH`` overrides for tests."""
    override = os.getenv("HFT_ALPHA_MINE_TRIAL_LEDGER_PATH")
    return Path(override) if override else _DEFAULT_JSONL_PATH


def _normalize_expression(expression: str) -> str:
    return " ".join(expression.split())


def _dedupe_key(trial_id: str, stage: str) -> str:
    return hashlib.sha256(f"{trial_id}:{stage}".encode("utf-8")).hexdigest()


def _semantic_identity(expression: str) -> str:
    """Canonical-hash identity, falling back to normalized text if malformed."""
    try:
        return canonical_hash(expression)
    except (SyntaxError, ValueError):
        return hashlib.sha256(_normalize_expression(expression).encode("utf-8")).hexdigest()


class TrialLedger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else _default_jsonl_path()
        self._cache: set[str] = set()
        self._warmed = False

    @staticmethod
    def candidate_id_for(expression: str) -> str:
        """Dataset-independent identity for a candidate expression (freeze tracking)."""
        return _semantic_identity(expression)

    @staticmethod
    def trial_id_for(
        *,
        expression: str,
        dataset_fingerprint: str,
        partition_manifest_hash: str,
        algorithm: str,
    ) -> str:
        """Trial identity (see module docstring)."""
        identity = _semantic_identity(expression)
        payload = f"{identity}:{dataset_fingerprint}:{partition_manifest_hash}:{algorithm}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _warm(self) -> None:
        if self._warmed:
            return
        self._warmed = True
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = row.get("dedupe_key")
                if key:
                    self._cache.add(str(key))

    def record_trial(
        self,
        *,
        trial_id: str,
        search_run_id: str,
        candidate_id: str,
        stage: str,
        status: str,
        metrics: Mapping[str, Any],
        dataset_fingerprint: str = "",
        parent_ids: Sequence[str] = (),
        cost_ms: float = 0.0,
        failure_reason: str = "",
    ) -> bool:
        """Append one trial record. Idempotent on ``(trial_id, stage)``.

        Returns True iff a new row was appended (False on a dedupe hit —
        recording the same trial at the same stage twice is a no-op; a new
        stage for the same trial_id is a new row).
        """
        self._warm()
        key = _dedupe_key(trial_id, stage)
        if key in self._cache:
            return False

        row: dict[str, Any] = {
            "dedupe_key": key,
            "trial_id": trial_id,
            "search_run_id": search_run_id,
            "candidate_id": candidate_id,
            "dataset_fingerprint": dataset_fingerprint,
            "stage": stage,
            "status": status,
            "metrics": dict(metrics),
            "parent_ids": list(parent_ids),
            "cost_ms": float(cost_ms),
            "failure_reason": failure_reason,
            "recorded_at_ns": timebase.now_ns(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        self._cache.add(key)
        return True

    def record_compile_failure(
        self,
        *,
        expression: str,
        dataset_fingerprint: str,
        partition_manifest_hash: str,
        algorithm: str,
        search_run_id: str,
        reason: str,
    ) -> bool:
        """Convenience wrapper: ``stage="compile", status="error"``.

        Compile failures must be recorded too, not just successes — otherwise
        the trial count silently undercounts the true number of hypotheses
        tested.
        """
        candidate_id = self.candidate_id_for(expression)
        trial_id = self.trial_id_for(
            expression=expression,
            dataset_fingerprint=dataset_fingerprint,
            partition_manifest_hash=partition_manifest_hash,
            algorithm=algorithm,
        )
        return self.record_trial(
            trial_id=trial_id,
            search_run_id=search_run_id,
            candidate_id=candidate_id,
            stage="compile",
            status="error",
            metrics={},
            dataset_fingerprint=dataset_fingerprint,
            failure_reason=reason,
        )

    def read_trials(
        self,
        *,
        search_run_id: str | None = None,
        dataset_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read all trial rows (optionally filtered), in file append order."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if search_run_id is not None and row.get("search_run_id") != search_run_id:
                    continue
                if dataset_fingerprint is not None and row.get("dataset_fingerprint") != dataset_fingerprint:
                    continue
                out.append(row)
        return out

    def unique_trial_count(
        self,
        *,
        search_run_id: str | None = None,
        dataset_fingerprint: str | None = None,
    ) -> int:
        """Count distinct ``trial_id`` values under the given filters.

        This is the "actual trial count export" deliverable, consumed by the
        (unmodified) deflated-Sharpe multiple-testing logic.
        """
        ids: set[str] = {
            str(row["trial_id"])
            for row in self.read_trials(search_run_id=search_run_id, dataset_fingerprint=dataset_fingerprint)
            if "trial_id" in row
        }
        return len(ids)
