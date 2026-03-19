#!/usr/bin/env python3
"""Experiment Artifact GC — Unit 10.

Scans research/experiments/runs/ and research/experiments/validations/
and deletes entries older than --keep-days, while preserving the latest
--keep-latest entries per alpha.

Usage:
    uv run python scripts/experiment_gc.py [--dry-run] [--keep-days N] [--keep-latest N]
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup — structlog is not required for standalone scripts; use stdlib
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("experiment_gc")

# Directory name timestamp pattern used in validations/:
#   20260318T152526Z_6cbf7024
_TS_RE = re.compile(r"^(\d{8}T\d{6}Z)")
_TS_FMT = "%Y%m%dT%H%M%SZ"

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "research" / "experiments" / "runs"
VALIDATIONS_ROOT = REPO_ROOT / "research" / "experiments" / "validations"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dir_size_bytes(path: Path) -> int:
    """Return total size in bytes of all files under *path*."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _parse_dir_timestamp(directory: Path) -> datetime | None:
    """Try to extract a UTC datetime from the directory name.

    Accepts the validation format ``20260318T152526Z_<hash>``.
    Falls back to None if the name doesn't match.
    """
    m = _TS_RE.match(directory.name)
    if m:
        try:
            return datetime.strptime(m.group(1), _TS_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _dir_age(directory: Path) -> datetime:
    """Return the effective UTC datetime for a directory.

    Prefers embedded timestamp from directory name; falls back to mtime.
    """
    ts = _parse_dir_timestamp(directory)
    if ts is not None:
        return ts
    mtime = directory.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Runs root (UUID-named, grouped by alpha via meta.json)
# ---------------------------------------------------------------------------


def _read_alpha_id(run_dir: Path) -> str:
    """Read alpha_id from meta.json; fall back to '_unknown'."""
    meta = run_dir / "meta.json"
    if meta.is_file():
        try:
            import json

            data = json.loads(meta.read_text(encoding="utf-8"))
            alpha = data.get("alpha_id")
            if alpha and isinstance(alpha, str):
                return alpha
        except Exception:  # noqa: BLE001
            pass
    return "_unknown"


def _collect_runs(
    runs_root: Path,
) -> dict[str, list[tuple[datetime, Path]]]:
    """Return mapping of alpha_id -> sorted list of (age_dt, path)."""
    by_alpha: dict[str, list[tuple[datetime, Path]]] = {}
    if not runs_root.is_dir():
        log.warning("runs root does not exist", extra={"path": str(runs_root)})
        return by_alpha

    for entry in runs_root.iterdir():
        if not entry.is_dir():
            continue
        alpha_id = _read_alpha_id(entry)
        age = _dir_age(entry)
        by_alpha.setdefault(alpha_id, []).append((age, entry))

    # Sort each alpha's runs oldest-first
    for alpha_id in by_alpha:
        by_alpha[alpha_id].sort(key=lambda t: t[0])

    return by_alpha


# ---------------------------------------------------------------------------
# Validations root (alpha-named subdirs → timestamp-named run dirs)
# ---------------------------------------------------------------------------


def _collect_validations(
    validations_root: Path,
) -> dict[str, list[tuple[datetime, Path]]]:
    """Return mapping of alpha_id -> sorted list of (age_dt, path)."""
    by_alpha: dict[str, list[tuple[datetime, Path]]] = {}
    if not validations_root.is_dir():
        log.warning(
            "validations root does not exist",
            extra={"path": str(validations_root)},
        )
        return by_alpha

    for alpha_dir in validations_root.iterdir():
        if not alpha_dir.is_dir():
            continue
        alpha_id = alpha_dir.name
        runs: list[tuple[datetime, Path]] = []
        for run_dir in alpha_dir.iterdir():
            if not run_dir.is_dir():
                continue
            age = _dir_age(run_dir)
            runs.append((age, run_dir))
        if runs:
            runs.sort(key=lambda t: t[0])
            by_alpha[alpha_id] = runs

    return by_alpha


# ---------------------------------------------------------------------------
# GC logic
# ---------------------------------------------------------------------------


def _select_deletions(
    by_alpha: dict[str, list[tuple[datetime, Path]]],
    cutoff: datetime,
    keep_latest: int,
) -> list[Path]:
    """Return list of directories to delete.

    Rules (applied per alpha):
    1. Always preserve the latest *keep_latest* entries — never delete them.
    2. Delete entries older than *cutoff* that are not in the preserved set.
    3. If total entries <= keep_latest, skip entirely (safety).
    """
    to_delete: list[Path] = []

    for alpha_id, runs in by_alpha.items():
        total = len(runs)
        if total <= keep_latest:
            log.debug(
                "skipping alpha — fewer entries than keep_latest",
                extra={"alpha": alpha_id, "count": total, "keep_latest": keep_latest},
            )
            continue

        # Preserve the most recent keep_latest entries (end of list = newest)
        protected_indices = set(range(total - keep_latest, total))

        for idx, (age, path) in enumerate(runs):
            if idx in protected_indices:
                continue
            if age < cutoff:
                to_delete.append(path)

    return to_delete


def run_gc(
    *,
    keep_days: int,
    keep_latest: int,
    dry_run: bool,
    runs_root: Path,
    validations_root: Path,
) -> None:
    """Main GC routine."""
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=keep_days)

    log.info(
        "experiment GC starting",
        extra={
            "keep_days": keep_days,
            "keep_latest": keep_latest,
            "dry_run": dry_run,
            "cutoff": cutoff.strftime(_TS_FMT),
        },
    )

    # Collect candidates from both roots
    runs_by_alpha = _collect_runs(runs_root)
    validations_by_alpha = _collect_validations(validations_root)

    total_dirs_scanned = sum(len(v) for v in runs_by_alpha.values()) + sum(
        len(v) for v in validations_by_alpha.values()
    )

    # Merge into one collection for uniform reporting; prefix alpha with source
    # to avoid collisions (runs and validations use the same alpha names)
    def _prefixed(
        source: str,
        by_alpha: dict[str, list[tuple[datetime, Path]]],
    ) -> dict[str, list[tuple[datetime, Path]]]:
        return {f"{source}:{k}": v for k, v in by_alpha.items()}

    combined = {
        **_prefixed("runs", runs_by_alpha),
        **_prefixed("validations", validations_by_alpha),
    }

    to_delete = _select_deletions(combined, cutoff, keep_latest)

    if not to_delete:
        log.info(
            "nothing to delete",
            extra={"total_scanned": total_dirs_scanned},
        )
    else:
        log.info(
            "directories selected for deletion",
            extra={"count": len(to_delete), "dry_run": dry_run},
        )

    dirs_deleted = 0
    bytes_freed = 0

    for path in to_delete:
        size = _dir_size_bytes(path)
        if dry_run:
            log.info("DRY-RUN would delete", extra={"path": str(path), "size_bytes": size})
        else:
            try:
                shutil.rmtree(path)
                log.info("deleted", extra={"path": str(path), "size_bytes": size})
                dirs_deleted += 1
                bytes_freed += size
            except OSError as exc:
                log.error("failed to delete", extra={"path": str(path), "error": str(exc)})

    if dry_run:
        dry_bytes = sum(_dir_size_bytes(p) for p in to_delete)
        log.info(
            "DRY-RUN summary",
            extra={
                "total_dirs_scanned": total_dirs_scanned,
                "dirs_that_would_be_deleted": len(to_delete),
                "space_that_would_be_freed_bytes": dry_bytes,
                "space_that_would_be_freed_mb": round(dry_bytes / 1_048_576, 2),
            },
        )
    else:
        log.info(
            "GC complete",
            extra={
                "total_dirs_scanned": total_dirs_scanned,
                "dirs_deleted": dirs_deleted,
                "space_freed_bytes": bytes_freed,
                "space_freed_mb": round(bytes_freed / 1_048_576, 2),
            },
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Garbage-collect old experiment artifacts from research/experiments/.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--keep-days",
        type=int,
        default=90,
        help="Delete entries older than this many days.",
    )
    parser.add_argument(
        "--keep-latest",
        type=int,
        default=3,
        help="Always preserve at least this many recent entries per alpha.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be deleted without actually deleting.",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=RUNS_ROOT,
        help="Override path to research/experiments/runs/.",
    )
    parser.add_argument(
        "--validations-root",
        type=Path,
        default=VALIDATIONS_ROOT,
        help="Override path to research/experiments/validations/.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.keep_days < 1:
        log.error("--keep-days must be at least 1")
        return 2

    if args.keep_latest < 1:
        log.error("--keep-latest must be at least 1")
        return 2

    run_gc(
        keep_days=args.keep_days,
        keep_latest=args.keep_latest,
        dry_run=args.dry_run,
        runs_root=args.runs_root,
        validations_root=args.validations_root,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
