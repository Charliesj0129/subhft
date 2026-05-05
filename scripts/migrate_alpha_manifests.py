"""Slice-D Task 15: backfill the kill ledger from the 2026-04-17 archive.

Walks ``research/archive/alphas_2026-04-17/``:
  * 25 dirs with ``manifest.yaml`` -> write a kill ledger row via
    ``kill_ledger.append_kill`` (dedupe-aware; idempotent).
  * 21 dirs without ``manifest.yaml`` -> write a row to
    ``research/archive/_kill_summary_2026-04-17.jsonl`` (sidecar).

Default is ``--dry-run`` (no writes). Pass ``--apply`` to write.

This is a one-shot migration script. Re-running with the same archive
contents is idempotent: ``kill_ledger.append_kill`` dedupes on
``(alpha_id, kill_id)`` and the summary writer dedupes on ``alpha_id``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = REPO_ROOT / "research" / "archive" / "alphas_2026-04-17"
SUMMARY_JSONL = REPO_ROOT / "research" / "archive" / "_kill_summary_2026-04-17.jsonl"
KILLED_AT_ISO = "2026-04-17T00:00:00Z"
# 2026-04-17T00:00:00Z in ns since epoch. Hardcoded so the migration is
# deterministic and idempotent across re-runs.
KILLED_AT_NS = 1_776_384_000_000_000_000

DEFAULT_REASON = "archived_2026_04_17"
NO_MANIFEST_REASON = "archived_2026_04_17_no_manifest"


def _parse_manifest(manifest_path: Path) -> tuple[dict[str, Any] | None, str]:
    """Return ``(manifest_dict, reason)`` or ``(None, fallback_reason)``.

    Best-effort: a parse error returns ``(None, DEFAULT_REASON)``.
    """
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None, DEFAULT_REASON
    if not isinstance(data, dict):
        return None, DEFAULT_REASON
    return data, _derive_reason(data)


def _derive_reason(manifest: dict[str, Any]) -> str:
    """Pull a human-meaningful reason from manifest fields, falling back to default."""
    for key in ("kill_reason", "status_reason", "notes"):
        v = manifest.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:512]
    return DEFAULT_REASON


def _stable_artifact_hash_from_manifest_dict(d: dict[str, Any]) -> str:
    """Hash the manifest dict directly.

    Don't reconstruct ``AlphaManifest``: archived manifests pre-date the
    Slice-D schema and may have extra fields or missing required ones.
    """
    excluded = {"kill_reason", "cluster_id"}
    payload = {k: v for k, v in d.items() if k not in excluded}
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scan(archive_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return ``(ledger_rows_to_write, summary_rows_to_write)``."""
    ledger_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, str]] = []

    for entry in sorted(archive_dir.iterdir()):
        if not entry.is_dir():
            continue
        alpha_id = entry.name
        manifest_path = entry / "manifest.yaml"
        if manifest_path.exists():
            data, reason = _parse_manifest(manifest_path)
            artifact_hash = (
                _stable_artifact_hash_from_manifest_dict(data) if data else ""
            )
            ledger_rows.append(
                {
                    "alpha_id": alpha_id,
                    "gate": "manual",  # archive sweep treated as manual decommission
                    "reason": reason,
                    "stable_artifact_hash": artifact_hash,
                    "scorecard_id": "",
                    "killed_by": "migration:archive_2026_04_17",
                    "killed_at": KILLED_AT_NS,
                }
            )
        else:
            summary_rows.append(
                {
                    "alpha_id": alpha_id,
                    "killed_at_iso": KILLED_AT_ISO,
                    "reason": NO_MANIFEST_REASON,
                }
            )

    return ledger_rows, summary_rows


def _write_summary_jsonl(rows: list[dict[str, str]], path: Path) -> int:
    """Write summary jsonl. Idempotent: dedupes on ``alpha_id``.

    Returns the number of newly-appended rows.
    """
    existing: set[str] = set()
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            alpha_id = str(row.get("alpha_id", ""))
            if alpha_id:
                existing.add(alpha_id)

    new_rows = [r for r in rows if r["alpha_id"] not in existing]
    if not new_rows:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in new_rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return len(new_rows)


def _apply(
    ledger_rows: list[dict[str, Any]],
    summary_rows: list[dict[str, str]],
    summary_path: Path,
) -> tuple[int, int]:
    """Write rows. Returns ``(inserted_ledger, inserted_summary)``."""
    # Imported here so the dry-run path doesn't pull in the full alpha stack.
    from hft_platform.alpha.kill_ledger import KillRecord, append_kill

    inserted_ledger = 0
    for r in ledger_rows:
        record = KillRecord(
            alpha_id=r["alpha_id"],
            gate=r["gate"],
            reason=r["reason"],
            stable_artifact_hash=r["stable_artifact_hash"],
            scorecard_id=r["scorecard_id"],
            killed_by=r["killed_by"],
            killed_at=r["killed_at"],
        )
        if append_kill(record):
            inserted_ledger += 1

    inserted_summary = _write_summary_jsonl(summary_rows, summary_path)
    return inserted_ledger, inserted_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write (default: dry run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request dry-run mode (the default; provided for clarity)",
    )
    parser.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR)
    parser.add_argument("--summary-path", type=Path, default=SUMMARY_JSONL)
    args = parser.parse_args()
    if args.dry_run and args.apply:
        print("--dry-run and --apply are mutually exclusive", file=sys.stderr)
        return 2

    if not args.archive_dir.exists():
        print(f"Archive dir not found: {args.archive_dir}", file=sys.stderr)
        return 1

    ledger_rows, summary_rows = _scan(args.archive_dir)

    print(f"Found {len(ledger_rows)} archived alphas with manifest.yaml")
    print(f"Found {len(summary_rows)} archived alphas without manifest.yaml")

    if not args.apply:
        print("DRY RUN -- no writes. Pass --apply to commit.")
        return 0

    inserted_ledger, inserted_summary = _apply(
        ledger_rows, summary_rows, args.summary_path
    )

    print(f"Inserted {inserted_ledger} ledger rows (rest were duplicates)")
    print(f"Inserted {inserted_summary} summary rows (rest were duplicates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
