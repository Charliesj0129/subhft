"""Stage-6 alpha lifecycle drift audit (D6).

Declares ``manifest.yaml::status`` as the **single source of truth** for an
alpha's lifecycle and cross-checks every derived store for drift:

  1. **Filesystem placement** — directory under ``research/alphas/`` (active)
     vs ``research/archive/alphas_*/`` (archived).
  2. **Manifest ``status`` field** — terminal statuses (``KILLED``,
     ``DEPRECATED``) must live under ``research/archive/``; active dirs must
     not carry a terminal status.
  3. **Kill ledger jsonl** (``research/alphas/_kill_ledger.jsonl``) — every
     ``alpha_id`` in the ledger must either be archived or have a terminal
     manifest status; orphan ledger rows (alpha_id with no manifest anywhere)
     are reported but tolerated (kills can pre-date archival cleanup).
  4. **Cluster assignments** (``research/alphas/_cluster_assignments.json``)
     — referenced alpha_ids should resolve to a known manifest; orphan
     references are reported.
  5. **Paper-index reverse links** (``research/knowledge/paper_index.json``,
     ``alphas:`` list per paper entry) — same orphan-reference rule.

ClickHouse ``audit.alpha_kill_ledger`` is **not** queried here (the audit
runs offline and must not depend on a live CH). The jsonl is the offline
mirror and is the authoritative offline source.

Exit codes:
  * 0 — zero drift.
  * 1 — at least one drift item.
  * 2 — IO error or malformed input.

Run via ``make research-audit-lifecycle`` (the canonical entrypoint).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Terminal statuses imply the manifest belongs in archive/.  PARKED and
# EXPLORATORY are *not* terminal — they remain candidates for revival.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"KILLED", "DEPRECATED"})

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_DIR = REPO_ROOT / "research" / "alphas"
ARCHIVE_ROOT = REPO_ROOT / "research" / "archive"
KILL_LEDGER = ACTIVE_DIR / "_kill_ledger.jsonl"
CLUSTER_ASSIGNMENTS = ACTIVE_DIR / "_cluster_assignments.json"
PAPER_INDEX = REPO_ROOT / "research" / "knowledge" / "paper_index.json"


@dataclass(frozen=True)
class ManifestRecord:
    alpha_id: str
    status: str  # raw string; we do NOT go through AlphaStatus enum here
    path: Path
    is_active: bool  # True if under research/alphas/<id>/; False if under research/archive/


@dataclass
class DriftReport:
    items: list[tuple[str, str, str]] = field(default_factory=list)  # (severity, code, message)

    def add(self, severity: str, code: str, message: str) -> None:
        self.items.append((severity, code, message))

    def errors(self) -> list[tuple[str, str, str]]:
        return [i for i in self.items if i[0] == "ERROR"]

    def warnings(self) -> list[tuple[str, str, str]]:
        return [i for i in self.items if i[0] == "WARN"]


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------

def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(body, dict):
        return None
    return body


def collect_manifests() -> tuple[dict[str, ManifestRecord], DriftReport]:
    """Walk active + archive trees; return alpha_id → ManifestRecord."""
    report = DriftReport()
    by_id: dict[str, ManifestRecord] = {}

    for manifest in ACTIVE_DIR.glob("*/manifest.yaml"):
        body = _read_manifest(manifest)
        if body is None:
            report.add("ERROR", "manifest_parse_error", f"{manifest.relative_to(REPO_ROOT)}")
            continue
        alpha_id = str(body.get("alpha_id") or manifest.parent.name)
        status = str(body.get("status") or "DRAFT")
        if alpha_id in by_id:
            report.add(
                "ERROR",
                "duplicate_alpha_id",
                f"{alpha_id} appears in both {by_id[alpha_id].path.relative_to(REPO_ROOT)} "
                f"and {manifest.relative_to(REPO_ROOT)}",
            )
            continue
        by_id[alpha_id] = ManifestRecord(alpha_id, status, manifest, is_active=True)

    for manifest in ARCHIVE_ROOT.glob("alphas_*/*/manifest.yaml"):
        body = _read_manifest(manifest)
        if body is None:
            report.add("ERROR", "manifest_parse_error", f"{manifest.relative_to(REPO_ROOT)}")
            continue
        alpha_id = str(body.get("alpha_id") or manifest.parent.name)
        status = str(body.get("status") or "DRAFT")
        if alpha_id in by_id:
            # An archived copy duplicating an active alpha is a drift signal.
            report.add(
                "ERROR",
                "active_and_archived",
                f"{alpha_id} present in {by_id[alpha_id].path.relative_to(REPO_ROOT)} "
                f"AND {manifest.relative_to(REPO_ROOT)} — pick one.",
            )
            continue
        by_id[alpha_id] = ManifestRecord(alpha_id, status, manifest, is_active=False)

    return by_id, report


# ---------------------------------------------------------------------------
# Cross-checks against derived stores
# ---------------------------------------------------------------------------

def check_status_placement(manifests: dict[str, ManifestRecord], report: DriftReport) -> None:
    """Active dirs must not carry a terminal status; archive dirs should."""
    for rec in manifests.values():
        if rec.is_active and rec.status.upper() in _TERMINAL_STATUSES:
            report.add(
                "ERROR",
                "terminal_in_active",
                f"{rec.alpha_id} status={rec.status} but lives in "
                f"{rec.path.parent.relative_to(REPO_ROOT)} — move to research/archive/.",
            )
        # We do NOT flag non-terminal-in-archive: an alpha may be archived
        # for housekeeping reasons (e.g. session sweeps) without a kill.


def check_kill_ledger(manifests: dict[str, ManifestRecord], report: DriftReport) -> set[str]:
    """Returns set of alpha_ids found in ledger."""
    ids: set[str] = set()
    if not KILL_LEDGER.exists():
        report.add("WARN", "kill_ledger_missing", str(KILL_LEDGER.relative_to(REPO_ROOT)))
        return ids
    try:
        for line in KILL_LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            aid = str(row.get("alpha_id", ""))
            if aid:
                ids.add(aid)
    except (OSError, json.JSONDecodeError) as exc:
        report.add("ERROR", "kill_ledger_parse_error", f"{exc}")
        return ids

    for aid in ids:
        rec = manifests.get(aid)
        if rec is None:
            report.add(
                "WARN",
                "ledger_orphan",
                f"{aid} in kill ledger but no manifest under research/alphas/ or research/archive/.",
            )
            continue
        if rec.is_active and rec.status.upper() not in _TERMINAL_STATUSES:
            report.add(
                "ERROR",
                "killed_but_active",
                f"{aid} has kill-ledger row but manifest is still active "
                f"(path={rec.path.parent.relative_to(REPO_ROOT)}, status={rec.status}).",
            )
    return ids


def check_cluster_assignments(manifests: dict[str, ManifestRecord], report: DriftReport) -> None:
    if not CLUSTER_ASSIGNMENTS.exists():
        return  # silent: optional
    try:
        body = json.loads(CLUSTER_ASSIGNMENTS.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as exc:
        report.add("ERROR", "cluster_assignments_parse_error", f"{exc}")
        return
    referenced: set[str] = set()
    if isinstance(body, dict):
        for entries in body.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    aid = str(entry.get("alpha_id", ""))
                    if aid:
                        referenced.add(aid)
    missing = sorted(aid for aid in referenced if aid not in manifests)
    if missing:
        # Cluster snapshots can lag; warn rather than error.
        report.add(
            "WARN",
            "cluster_orphan_refs",
            f"{len(missing)} alpha_ids in _cluster_assignments.json without manifests "
            f"(e.g. {missing[:3]}). Snapshot may be stale.",
        )


def check_paper_index(manifests: dict[str, ManifestRecord], report: DriftReport) -> None:
    if not PAPER_INDEX.exists():
        return
    try:
        body = json.loads(PAPER_INDEX.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError) as exc:
        report.add("ERROR", "paper_index_parse_error", f"{exc}")
        return
    refs_by_paper: dict[str, list[str]] = defaultdict(list)
    if isinstance(body, dict):
        for paper_id, entry in body.items():
            if not isinstance(entry, dict):
                continue
            for aid in entry.get("alphas", []) or []:
                refs_by_paper[str(paper_id)].append(str(aid))
    orphans = [
        (paper_id, aid)
        for paper_id, aids in refs_by_paper.items()
        for aid in aids
        if aid not in manifests
    ]
    if orphans:
        sample = orphans[:3]
        report.add(
            "WARN",
            "paper_index_orphan_refs",
            f"{len(orphans)} paper_index.json reverse links point at unknown alphas "
            f"(sample: {sample}).",
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def run_audit(*, json_out: Path | None = None) -> int:
    manifests, report = collect_manifests()
    check_status_placement(manifests, report)
    check_kill_ledger(manifests, report)
    check_cluster_assignments(manifests, report)
    check_paper_index(manifests, report)

    errors = report.errors()
    warnings = report.warnings()

    print(f"[lifecycle_audit] manifests={len(manifests)} errors={len(errors)} warnings={len(warnings)}")
    for severity, code, message in report.items:
        print(f"  [{severity}] {code}: {message}")

    if json_out is not None:
        json_out.write_text(
            json.dumps(
                {
                    "manifest_count": len(manifests),
                    "errors": [{"code": c, "message": m} for _, c, m in errors],
                    "warnings": [{"code": c, "message": m} for _, c, m in warnings],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-check alpha lifecycle state across derived stores.")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON report path.")
    args = parser.parse_args()
    try:
        return run_audit(json_out=args.json_out)
    except Exception as exc:  # noqa: BLE001 — top-level CLI guard
        print(f"[lifecycle_audit] fatal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
