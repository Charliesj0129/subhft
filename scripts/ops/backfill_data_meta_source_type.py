"""One-shot backfill of `source_type` for `research/data/raw/**/*.meta.json`.

Historical writer (``research.data_pipeline.export_l2_ticks``, source-removed
but bytecode-only present) stamped ``source_type`` with the payload-kind
("tick" / "l2_hftbacktest"). The governance validator expects the provenance
label {"synthetic", "real"} (see ``research/tools/data_governance.py:57``).

This script:
  * walks ``research/data/raw/``,
  * for each ``*.meta.json``, moves the legacy payload tag into a sibling
    ``data_kind`` field and stamps ``source_type: "real"`` (CK exports),
  * skips files already conforming,
  * recomputes ``data_fingerprint`` if absent,
  * writes the JSON report under ``research/reports/``.

Run once. Idempotent. Atomic per-file via tmp+rename.

Usage:
    uv run python scripts/ops/backfill_data_meta_source_type.py [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "research" / "data" / "raw"
REPORT_DIR = REPO_ROOT / "research" / "reports"
VALID_PROVENANCE = {"synthetic", "real"}
LEGACY_KIND_FIELD = "data_kind"


def _fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _process_one(meta_path: Path, *, dry_run: bool) -> str:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"error:{exc}"

    current = str(meta.get("source_type", "")).lower()
    changed = False

    if current in VALID_PROVENANCE:
        # Already conforming — only backfill fingerprint if missing.
        if "data_fingerprint" not in meta:
            data_file = meta_path.parent / meta.get("data_file", meta_path.stem.rstrip(".meta"))
            if data_file.exists():
                meta["data_fingerprint"] = _fingerprint(data_file)
                changed = True
        return "ok_changed" if changed else "ok"

    # Non-conforming: stash legacy tag, set provenance.
    if current:
        meta.setdefault(LEGACY_KIND_FIELD, current)
    meta["source_type"] = "real"
    changed = True

    if "data_fingerprint" not in meta:
        data_file = meta_path.parent / meta.get("data_file", meta_path.stem.rstrip(".meta"))
        if data_file.exists():
            meta["data_fingerprint"] = _fingerprint(data_file)

    if dry_run:
        return "would_update"

    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, meta_path)
    return "updated"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=RAW_ROOT,
        help="Override raw-data root for testing",
    )
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"raw root not found: {args.root}", file=sys.stderr)
        return 2

    counters = {"updated": 0, "would_update": 0, "ok": 0, "ok_changed": 0, "error": 0}
    files = sorted(args.root.rglob("*.meta.json"))
    for f in files:
        result = _process_one(f, dry_run=args.dry_run)
        key = result.split(":", 1)[0]
        counters[key] = counters.get(key, 0) + 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "root": str(args.root),
        "dry_run": args.dry_run,
        "total_files": len(files),
        "counters": counters,
    }
    report_path = REPORT_DIR / "backfill_data_meta_source_type.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
