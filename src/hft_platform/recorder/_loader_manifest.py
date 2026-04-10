"""WAL manifest tracking helpers for WALLoaderService.

Extracted from loader.py for cohesion. All functions receive ``self``
(a WALLoaderService instance) as the first argument and are bound as
methods by the main module.
"""

from __future__ import annotations

import glob as _glob
import os
import shutil
from typing import TYPE_CHECKING

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.recorder.loader import WALLoaderService

logger = get_logger("wal_loader")


def _load_manifest(self: WALLoaderService) -> None:
    """Load processed file manifest from disk (P1-1).

    EC-5: Validates manifest against actual WAL directory to detect
    stuck files (in manifest but still pending in WAL dir).
    """
    if not os.path.exists(self._manifest_path):
        self._manifest = set()
        return
    try:
        with open(self._manifest_path, "r") as f:
            self._manifest = {line.strip() for line in f if line.strip()}
        logger.info("Loaded WAL manifest", count=len(self._manifest))
    except Exception as e:
        logger.warning("Failed to load manifest, starting fresh", error=str(e))
        self._manifest = set()
        return

    # EC-5: Detect stuck files still in WAL dir but marked as processed
    try:
        pending = {f for f in os.listdir(self.wal_dir) if f.endswith(".jsonl")}
        stuck = self._manifest & pending
        if stuck:
            logger.warning(
                "Manifest has entries still pending in WAL dir, allowing re-process",
                count=len(stuck),
            )
            self._manifest -= stuck
    except OSError:
        pass


def _save_manifest(self: WALLoaderService) -> None:
    """Save processed file manifest to disk atomically (P1-1, EC-5).

    Uses temp file + fsync + rename to prevent corruption on crash.
    """
    manifest_dir = os.path.dirname(self._manifest_path) or "."
    try:
        os.makedirs(manifest_dir, exist_ok=True)
        # Backup current manifest before overwrite
        if os.path.exists(self._manifest_path):
            bak_path = self._manifest_path + ".bak"
            try:
                shutil.copy2(self._manifest_path, bak_path)
            except OSError:
                pass
        # Atomic write via temp + rename
        import tempfile

        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=manifest_dir)
        try:
            with os.fdopen(fd, "w") as f:
                for fname in sorted(self._manifest):
                    f.write(fname + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, self._manifest_path)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except Exception as e:
        logger.warning("Failed to save manifest", error=str(e))


def _mark_processed(self: WALLoaderService, filename: str) -> None:
    """Mark file as processed in manifest (P1-1, CC-3 thread-safe)."""
    if not self._manifest_enabled:
        return
    fname = os.path.basename(filename)
    with self._manifest_lock:
        self._manifest.add(fname)


def _get_new_files(self: WALLoaderService) -> list[str]:
    """Get list of new WAL files not in manifest (P1-1, EC-2).

    Returns:
        List of full file paths for new files to process,
        sorted by embedded nanosecond timestamp.
    """
    if not self._manifest_enabled:
        files = _glob.glob(os.path.join(self.wal_dir, "*.jsonl"))
        files.sort(key=lambda p: _extract_file_ts(os.path.basename(p)))
        return files

    try:
        current = {f for f in os.listdir(self.wal_dir) if f.endswith(".jsonl")}
    except OSError:
        return []

    new_files = sorted(current - self._manifest, key=_extract_file_ts)
    return [os.path.join(self.wal_dir, f) for f in new_files]


def _extract_file_ts(fname: str) -> int:
    """Extract nanosecond timestamp from WAL filename (EC-2).

    Filename formats:
    - {table}_{nanosecond_ts}.jsonl
    - batch_{nanosecond_ts}_{sequence}.jsonl

    Returns 0 if parsing fails.
    """
    try:
        base = fname.rsplit(".", 1)[0]  # strip .jsonl
        if base.startswith("batch_"):
            parts = base.split("_")
            if len(parts) >= 3:
                return int(parts[1])
        ts_str = base.rsplit("_", 1)[-1]
        return int(ts_str)
    except (ValueError, IndexError):
        return 0
