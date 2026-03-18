"""Dead-letter-queue write/replay and periodic cleanup helpers.

Functions operate on a ``WALLoaderService`` instance passed as the first
argument so that the public API stays in ``loader.py``.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from hft_platform.recorder._loader_common import (
    _dumps,
    _loads,
    logger,
    timebase,
)

# ---------------------------------------------------------------------------
# DLQ write
# ---------------------------------------------------------------------------


def write_to_dlq(
    svc: Any,
    table: str,
    rows: list[dict[str, Any]],
    error: str,
) -> None:
    """Write failed rows to Dead Letter Queue for later analysis."""
    os.makedirs(svc.dlq_dir, exist_ok=True)
    ts = int(timebase.now_ns())
    dlq_file = os.path.join(svc.dlq_dir, f"{table}_{ts}.jsonl")
    try:
        with open(dlq_file, "w") as f:
            f.write(
                _dumps(
                    {
                        "_dlq_meta": True,
                        "table": table,
                        "error": error,
                        "timestamp": ts,
                        "row_count": len(rows),
                    }
                )
                + "\n"
            )
            for row in rows:
                f.write(_dumps(row) + "\n")
        logger.warning(
            "Wrote failed batch to DLQ",
            table=table,
            count=len(rows),
            file=dlq_file,
        )
        if svc.metrics:
            try:
                svc.metrics.dlq_size_total.labels(source="recorder").inc()
            except Exception:
                pass
    except Exception as e:
        logger.error("Failed to write to DLQ", table=table, error=str(e))


# ---------------------------------------------------------------------------
# DLQ replay
# ---------------------------------------------------------------------------


def replay_dlq(
    svc: Any,
    dry_run: bool = False,
    max_files: int | None = None,
) -> dict:
    """Replay DLQ files back into ClickHouse.

    Returns dict with keys ``replayed``, ``skipped``, ``failed``,
    ``errors``.
    """
    if not os.path.isdir(svc.dlq_dir):
        logger.info(
            "DLQ directory does not exist, nothing to replay",
            dlq_dir=svc.dlq_dir,
        )
        return {"replayed": 0, "skipped": 0, "failed": 0, "errors": []}

    if not svc.ch_client and not dry_run:
        logger.error("Cannot replay DLQ: no ClickHouse client available")
        return {
            "replayed": 0,
            "skipped": 0,
            "failed": 0,
            "errors": ["no_client"],
        }

    replayed = 0
    skipped = 0
    failed = 0
    errors: list[str] = []
    selected = 0

    file_names = sorted([f for f in os.listdir(svc.dlq_dir) if f.endswith(".jsonl")])
    if isinstance(max_files, int) and max_files > 0:
        file_names = file_names[:max_files]

    from hft_platform.recorder._loader_wal import parse_table_from_filename

    for fname in file_names:
        selected += 1
        fpath = os.path.join(svc.dlq_dir, fname)

        table = parse_table_from_filename(fname)
        if table == "unknown":
            logger.warning("DLQ replay: unknown table for file", file=fname)
            skipped += 1
            continue

        try:
            rows: list = []
            with open(fpath, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = _loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and obj.get("_dlq_meta"):
                        continue
                    rows.append(obj)

            if not rows:
                logger.info("DLQ replay: empty file, archiving", file=fname)
                if not dry_run:
                    shutil.move(fpath, os.path.join(svc.archive_dir, fname))
                skipped += 1
                continue

            logger.info(
                "DLQ replay: inserting rows",
                file=fname,
                table=table,
                rows=len(rows),
                dry_run=dry_run,
            )

            if dry_run:
                replayed += 1
                continue

            success = svc.insert_batch(table, rows)
            if success:
                shutil.move(fpath, os.path.join(svc.archive_dir, fname))
                logger.info(
                    "DLQ replay: success, archived",
                    file=fname,
                    rows=len(rows),
                )
                replayed += 1
            else:
                logger.error(
                    "DLQ replay: insert failed, leaving in DLQ",
                    file=fname,
                )
                failed += 1
                errors.append(fname)

        except Exception as e:
            logger.error(
                "DLQ replay: unexpected error",
                file=fname,
                error=str(e),
            )
            failed += 1
            errors.append(f"{fname}: {e}")

    logger.info(
        "DLQ replay complete",
        replayed=replayed,
        skipped=skipped,
        failed=failed,
        dry_run=dry_run,
        selected=selected,
        max_files=max_files,
    )
    return {
        "replayed": replayed,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "selected": selected,
        "max_files": max_files,
    }


# ---------------------------------------------------------------------------
# Cleanup helpers (B3, B5, archive)
# ---------------------------------------------------------------------------


def cleanup_old_dlq_files(svc: Any) -> None:
    """Remove or archive DLQ files older than retention period (B3)."""
    now = timebase.now_s()
    if now - svc._last_dlq_cleanup_ts < svc._dlq_cleanup_interval_s:
        return
    svc._last_dlq_cleanup_ts = now

    if not os.path.isdir(svc.dlq_dir):
        return

    retention_seconds = svc._dlq_retention_days * 86400
    cutoff_ts = now - retention_seconds
    archived = 0
    deleted = 0

    try:
        for fname in os.listdir(svc.dlq_dir):
            fpath = os.path.join(svc.dlq_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                mtime = os.path.getmtime(fpath)
                if mtime >= cutoff_ts:
                    continue

                if svc._dlq_archive_path:
                    os.makedirs(svc._dlq_archive_path, exist_ok=True)
                    dest = os.path.join(svc._dlq_archive_path, fname)
                    shutil.move(fpath, dest)
                    archived += 1
                else:
                    os.remove(fpath)
                    deleted += 1
            except Exception as e:
                logger.warning(
                    "Failed to clean up DLQ file",
                    file=fname,
                    error=str(e),
                )

        if archived or deleted:
            logger.info(
                "DLQ cleanup completed",
                archived=archived,
                deleted=deleted,
                retention_days=svc._dlq_retention_days,
            )
            if svc.metrics:
                try:
                    svc.metrics.dlq_size_total.labels(source="cleanup").inc(archived + deleted)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("DLQ cleanup failed", error=str(e))


def cleanup_old_corrupt_files(svc: Any) -> None:
    """Remove corrupt files older than retention period (B5)."""
    now = timebase.now_s()
    if now - svc._last_corrupt_cleanup_ts < svc._dlq_cleanup_interval_s:
        return
    svc._last_corrupt_cleanup_ts = now

    if not os.path.isdir(svc.corrupt_dir):
        return

    retention_seconds = svc._corrupt_retention_days * 86400
    cutoff_ts = now - retention_seconds
    deleted = 0

    try:
        for fname in os.listdir(svc.corrupt_dir):
            fpath = os.path.join(svc.corrupt_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                mtime = os.path.getmtime(fpath)
                if mtime >= cutoff_ts:
                    continue
                os.remove(fpath)
                deleted += 1
            except Exception as e:
                logger.warning(
                    "Failed to clean up corrupt file",
                    file=fname,
                    error=str(e),
                )

        if deleted:
            logger.info(
                "Corrupt file cleanup completed",
                deleted=deleted,
                retention_days=svc._corrupt_retention_days,
            )
    except Exception as e:
        logger.warning("Corrupt file cleanup failed", error=str(e))


def cleanup_old_archive_files(svc: Any) -> None:
    """Remove archived WAL files older than retention period."""
    now = timebase.now_s()
    if now - svc._last_archive_cleanup_ts < svc._dlq_cleanup_interval_s:
        return
    svc._last_archive_cleanup_ts = now

    if not os.path.isdir(svc.archive_dir):
        return

    retention_seconds = svc._archive_retention_days * 86400
    cutoff_ts = now - retention_seconds
    deleted = 0

    try:
        for fname in os.listdir(svc.archive_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(svc.archive_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                if os.path.getmtime(fpath) >= cutoff_ts:
                    continue
                os.remove(fpath)
                deleted += 1
            except Exception as exc:
                logger.warning(
                    "Failed to clean up archive WAL file",
                    file=fname,
                    error=str(exc),
                )

        if deleted:
            logger.info(
                "Archive file cleanup completed",
                deleted=deleted,
                retention_days=svc._archive_retention_days,
            )
    except Exception as exc:
        logger.warning("Archive file cleanup failed", error=str(exc))


# ---------------------------------------------------------------------------
# WAL accumulation monitoring (C5)
# ---------------------------------------------------------------------------


def check_wal_accumulation(svc: Any) -> None:
    """Check WAL directory size and emit metrics (C5)."""
    now = timebase.now_s()
    if now - svc._last_wal_check_ts < svc._wal_check_interval_s:
        return
    svc._last_wal_check_ts = now

    if not os.path.isdir(svc.wal_dir):
        return

    total_size = 0
    file_count = 0
    oldest_mtime = now

    try:
        for fname in os.listdir(svc.wal_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(svc.wal_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                stat = os.stat(fpath)
                total_size += stat.st_size
                file_count += 1
                oldest_mtime = min(oldest_mtime, stat.st_mtime)
            except OSError:
                continue

        if svc.metrics:
            svc.metrics.wal_directory_size_bytes.set(total_size)
            svc.metrics.wal_file_count.set(file_count)
            oldest_age = now - oldest_mtime if file_count else 0
            svc.metrics.wal_oldest_file_age_seconds.set(oldest_age)
            svc.metrics.wal_backlog_files.set(file_count)
            svc.metrics.wal_replay_lag_seconds.set(oldest_age)
            now_ts = now
            dt = max(1e-6, now_ts - svc._eta_sample_last_ts)
            delta_files = max(
                0,
                svc._processed_files_total - svc._eta_sample_last_processed,
            )
            files_per_s = (delta_files / dt) if delta_files > 0 else 0.0
            if file_count <= 0:
                eta_s = 0.0
            elif files_per_s > 0:
                eta_s = file_count / files_per_s
            else:
                eta_s = 0.0
            svc.metrics.wal_drain_eta_seconds.set(float(eta_s))
            svc._eta_sample_last_ts = now_ts
            svc._eta_sample_last_processed = svc._processed_files_total

        size_mb = total_size / (1024 * 1024)
        if size_mb > svc._wal_size_critical_mb:
            logger.critical(
                "WAL directory critically large",
                size_mb=round(size_mb, 2),
                file_count=file_count,
                threshold_mb=svc._wal_size_critical_mb,
            )
        elif size_mb > svc._wal_size_warning_mb:
            logger.warning(
                "WAL directory large",
                size_mb=round(size_mb, 2),
                file_count=file_count,
                threshold_mb=svc._wal_size_warning_mb,
            )
    except Exception as e:
        logger.warning("WAL accumulation check failed", error=str(e))


def quarantine_corrupt_file(svc: Any, fpath: str, fname: str, reason: str) -> None:
    """Move corrupt WAL file to quarantine directory."""
    os.makedirs(svc.corrupt_dir, exist_ok=True)
    try:
        dest_path = os.path.join(svc.corrupt_dir, fname)
        shutil.move(fpath, dest_path)
        logger.error(
            "Moved corrupt WAL to quarantine",
            file=fname,
            reason=reason,
            dest=dest_path,
        )
    except Exception as e:
        logger.error(
            "Failed to quarantine corrupt file",
            file=fname,
            error=str(e),
        )
