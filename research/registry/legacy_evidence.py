"""Read-only adapter over pre-ledger research evidence (ADR-005 section 6).

``research/experiments/**`` is frozen: nothing here writes to it. The adapter
reads its run metas and the pre-ledger kills (the ``_kill_ledger.jsonl``
fallback and ClickHouse ``audit.alpha_kill_ledger``) and quarantines rows that
tests wrote into the real sinks before the isolation fixture existed.

A row is quarantined, with the reason kept beside it, when:

- ``pytest_tmp_path``: any of its text names a pytest temporary directory;
- ``unknown_alpha``: it was written by automation (a tracker run, or a kill
  whose ``killed_by`` is not a ``cli:`` operator) for an alpha that has no
  package under ``research/alphas`` or ``research/archive``.

Operator kills (``killed_by`` ``cli:...``) of an alpha without a package are
kept: abandoned scaffolds were killed by hand before they had a manifest.
Quarantined rows are counted, never deleted.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hft_platform.alpha.paths import repo_path

_PYTEST_MARKERS = ("pytest-of-", "/pytest-")
_CH_KILL_COLUMNS = (
    "kill_id",
    "killed_at",
    "alpha_id",
    "gate",
    "reason",
    "stable_artifact_hash",
    "scorecard_id",
    "killed_by",
)


@dataclass(frozen=True, slots=True)
class LegacyRun:
    alpha_id: str
    run_id: str
    timestamp: str
    quarantine: str | None


def research_root() -> Path:
    return repo_path("research")


def known_alpha_ids(root: Path | None = None) -> frozenset[str]:
    """Alpha ids with a package under ``alphas/`` or one level into an ``archive/`` batch."""
    root = root if root is not None else research_root()
    ids = {p.name for p in (root / "alphas").glob("*") if p.is_dir() and not p.name.startswith(("_", "."))}
    ids |= {p.name for p in (root / "archive").glob("*/*") if p.is_dir()}
    return frozenset(ids)


def quarantine_reason(row: Mapping[str, Any], *, automated: bool, known: frozenset[str]) -> str | None:
    """Why ``row`` is a test fixture rather than evidence, or None."""
    text = json.dumps(row, default=str)
    if any(marker in text for marker in _PYTEST_MARKERS):
        return "pytest_tmp_path"
    if automated and str(row.get("alpha_id", "")) not in known:
        return "unknown_alpha"
    return None


def read_runs(runs_root: Path, known: frozenset[str], *, quarantine: bool = True) -> list[LegacyRun]:
    """One row per ``<runs_root>/*/meta.json`` with an alpha id, in path order.

    ``quarantine`` applies the fixture rules; governed runs under the evidence
    root were written after test isolation and are read with it off. An
    unreadable meta is quarantined as ``unreadable_meta``.
    """
    runs: list[LegacyRun] = []
    for meta_path in sorted(runs_root.glob("*/meta.json")):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            runs.append(LegacyRun("", meta_path.parent.name, "", "unreadable_meta"))
            continue
        if not isinstance(payload, dict) or not str(payload.get("alpha_id", "")).strip():
            continue
        reason = quarantine_reason(payload, automated=True, known=known) if quarantine else None
        runs.append(
            LegacyRun(
                alpha_id=str(payload["alpha_id"]).strip(),
                run_id=str(payload.get("run_id", "")).strip(),
                timestamp=str(payload.get("timestamp", "")).strip(),
                quarantine=reason,
            )
        )
    return runs


def run_stats(runs: Iterable[LegacyRun]) -> dict[str, dict[str, Any]]:
    """Per alpha: ``run_count``, ``latest_run_id``, ``latest_timestamp`` and ``quarantined_count``."""
    per_alpha: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not run.alpha_id:
            continue
        row = per_alpha.setdefault(
            run.alpha_id, {"run_count": 0, "latest_run_id": "", "latest_timestamp": "", "quarantined_count": 0}
        )
        if run.quarantine:
            row["quarantined_count"] += 1
            continue
        row["run_count"] += 1
        if run.timestamp and run.timestamp >= row["latest_timestamp"]:
            row["latest_timestamp"] = run.timestamp
            row["latest_run_id"] = run.run_id
    return per_alpha


def _kill_row(row: Mapping[str, Any], source: str, known: frozenset[str]) -> dict[str, Any]:
    killed_at = row.get("killed_at")
    if isinstance(killed_at, datetime):
        killed_at = int(killed_at.replace(tzinfo=killed_at.tzinfo or UTC).timestamp() * 1_000_000_000)
    out = {
        "alpha_id": str(row.get("alpha_id", "")),
        "kill_id": str(row.get("kill_id", "")),
        "gate": str(row.get("gate", "")),
        "reason": str(row.get("reason", "")),
        "killed_by": str(row.get("killed_by", "system")),
        "killed_at": killed_at,
        "stable_artifact_hash": str(row.get("stable_artifact_hash", "")),
        "scorecard_id": str(row.get("scorecard_id", "")),
        "run_id": str(row.get("run_id", "")),
        "source": source,
    }
    automated = not out["killed_by"].startswith("cli:")
    out["quarantine"] = quarantine_reason(out, automated=automated, known=known)
    return out


def read_jsonl_kills(path: Path, known: frozenset[str]) -> list[dict[str, Any]]:
    """Kills from the ``_kill_ledger.jsonl`` fallback; a line that is not a kill raises ``ValueError``."""
    if not path.exists():
        return []
    kills: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: not JSON ({exc})") from exc
            if not isinstance(row, dict) or not row.get("alpha_id") or not row.get("kill_id"):
                raise ValueError(f"{path}:{lineno}: not a kill row")
            kills.append(_kill_row(row, "jsonl", known))
    return kills


def read_ch_kills(client: Any, known: frozenset[str]) -> list[dict[str, Any]]:
    """Every row of ``audit.alpha_kill_ledger`` (a SELECT; the table is never changed)."""
    columns = ", ".join(_CH_KILL_COLUMNS)
    result = client.query(f"SELECT {columns} FROM audit.alpha_kill_ledger ORDER BY killed_at, kill_id")
    return [_kill_row(dict(zip(_CH_KILL_COLUMNS, row, strict=True)), "clickhouse", known) for row in result.result_rows]


def combined_run_stats(evidence_root: Path, legacy_runs_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Per alpha run counts from both roots.

    ``legacy_run_count`` (``research/experiments/runs``, fixtures quarantined),
    ``governed_run_count`` (``<evidence root>/runs``), their sum ``run_count``,
    ``quarantined_run_count``, and the latest run across both.
    """
    legacy_root = legacy_runs_root if legacy_runs_root is not None else research_root() / "experiments" / "runs"
    known = known_alpha_ids()
    legacy = run_stats(read_runs(legacy_root, known))
    governed = run_stats(read_runs(evidence_root / "runs", known, quarantine=False))
    combined: dict[str, dict[str, Any]] = {}
    for alpha_id in sorted(set(legacy) | set(governed)):
        old = legacy.get(alpha_id, {})
        new = governed.get(alpha_id, {})
        latest = max((old, new), key=lambda s: str(s.get("latest_timestamp", "")))
        combined[alpha_id] = {
            "legacy_run_count": int(old.get("run_count", 0)),
            "governed_run_count": int(new.get("run_count", 0)),
            "run_count": int(old.get("run_count", 0)) + int(new.get("run_count", 0)),
            "quarantined_run_count": int(old.get("quarantined_count", 0)),
            "latest_run_id": str(latest.get("latest_run_id", "")),
            "latest_timestamp": str(latest.get("latest_timestamp", "")),
        }
    return combined
