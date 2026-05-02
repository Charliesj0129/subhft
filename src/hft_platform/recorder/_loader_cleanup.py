"""Cleanup helpers for WALLoaderService.

Extracted from loader.py for cohesion. All functions receive ``self``
(a WALLoaderService instance) as the first argument and are bound as
methods by the main module.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from structlog import get_logger

from hft_platform.core import timebase

if TYPE_CHECKING:
    from hft_platform.recorder.loader import WALLoaderService

logger = get_logger("wal_loader")


def _cleanup_old_corrupt_files(self: WALLoaderService) -> None:
    """Remove corrupt files older than retention period (B5)."""
    now = timebase.now_s()
    if now - self._last_corrupt_cleanup_ts < self._dlq_cleanup_interval_s:
        return
    self._last_corrupt_cleanup_ts = now

    if not os.path.isdir(self.corrupt_dir):
        return

    retention_seconds = self._corrupt_retention_days * 86400
    cutoff_ts = now - retention_seconds
    deleted = 0

    try:
        for fname in os.listdir(self.corrupt_dir):
            fpath = os.path.join(self.corrupt_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                mtime = os.path.getmtime(fpath)
                if mtime >= cutoff_ts:
                    continue
                os.remove(fpath)
                deleted += 1
            except Exception as e:
                logger.warning("Failed to clean up corrupt file", file=fname, error=str(e))

        if deleted:
            logger.info(
                "Corrupt file cleanup completed",
                deleted=deleted,
                retention_days=self._corrupt_retention_days,
            )
    except Exception as e:
        logger.warning("Corrupt file cleanup failed", error=str(e))


def _cleanup_old_archive_files(self: WALLoaderService) -> None:
    """Remove archived WAL files older than retention period to prevent unbounded disk growth."""
    now = timebase.now_s()
    if now - self._last_archive_cleanup_ts < self._dlq_cleanup_interval_s:
        return
    self._last_archive_cleanup_ts = now

    if not os.path.isdir(self.archive_dir):
        return

    retention_seconds = self._archive_retention_days * 86400
    cutoff_ts = now - retention_seconds
    deleted = 0

    try:
        for fname in os.listdir(self.archive_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(self.archive_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                if os.path.getmtime(fpath) >= cutoff_ts:
                    continue
                os.remove(fpath)
                deleted += 1
            except Exception as exc:
                logger.warning("Failed to clean up archive WAL file", file=fname, error=str(exc))

        if deleted:
            logger.info(
                "Archive file cleanup completed",
                deleted=deleted,
                retention_days=self._archive_retention_days,
            )
    except Exception as exc:
        logger.warning("Archive file cleanup failed", error=str(exc))


def _scan_dir_jsonl(path: str, now: float) -> tuple[int, int, float]:
    """Return (total_size_bytes, file_count, oldest_mtime) for *.jsonl files
    in a single (non-recursive) directory. Returns (0, 0, now) on missing/empty.

    P2-a (2026-04-27): split out so _check_wal_accumulation can scan each
    tier (active top-level, archive/, dlq/) independently and label the
    Prometheus gauges, instead of silently ignoring 3 GB of archived files.
    """
    if not os.path.isdir(path):
        return 0, 0, now
    total_size = 0
    file_count = 0
    oldest_mtime = now
    try:
        for fname in os.listdir(path):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(path, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                st = os.stat(fpath)
                total_size += st.st_size
                file_count += 1
                if st.st_mtime < oldest_mtime:
                    oldest_mtime = st.st_mtime
            except OSError:
                continue
    except OSError:
        # Directory disappeared mid-scan — return what we have.
        pass
    return total_size, file_count, oldest_mtime


def _check_wal_accumulation(self: WALLoaderService) -> None:
    """Check WAL directory size and emit metrics (C5).

    P2-a (2026-04-27): tier-aware scan. Pre-fix this only counted top-level
    *.jsonl files (active tier), missing 34 k+ files / 3 GB in archive/
    and any dlq/ accumulation. Live metrics reported `wal_directory_size_bytes=0`
    despite `du -sh /app/.wal=3.0G`. Now emits:
      * wal_directory_bytes{tier=active|archive|dlq}
      * wal_file_count_tiered{tier=...}
    Legacy unlabeled gauges (wal_directory_size_bytes, wal_file_count,
    wal_oldest_file_age_seconds) keep tracking the ACTIVE tier for back-compat.
    """
    now = timebase.now_s()
    if now - self._last_wal_check_ts < self._wal_check_interval_s:
        return
    self._last_wal_check_ts = now

    if not os.path.isdir(self.wal_dir):
        return

    try:
        # Per-tier non-recursive scans. archive/ and dlq/ live under wal_dir;
        # rglob would also find anything under archive/<subdir>/*.jsonl but
        # in practice neither directory is nested.
        active_size, active_count, active_oldest = _scan_dir_jsonl(self.wal_dir, now)
        archive_size, archive_count, archive_oldest = _scan_dir_jsonl(self.archive_dir, now)
        dlq_size, dlq_count, dlq_oldest = _scan_dir_jsonl(self.dlq_dir, now)

        # Emit metrics
        if self.metrics:
            # Tiered gauges (P2-a):
            self.metrics.wal_directory_bytes.labels(tier="active").set(active_size)
            self.metrics.wal_directory_bytes.labels(tier="archive").set(archive_size)
            self.metrics.wal_directory_bytes.labels(tier="dlq").set(dlq_size)
            self.metrics.wal_file_count_tiered.labels(tier="active").set(active_count)
            self.metrics.wal_file_count_tiered.labels(tier="archive").set(archive_count)
            self.metrics.wal_file_count_tiered.labels(tier="dlq").set(dlq_count)

            # Legacy unlabeled gauges — keep pointing at ACTIVE tier so
            # existing dashboards / alerts don't lose meaning.
            self.metrics.wal_directory_size_bytes.set(active_size)
            self.metrics.wal_file_count.set(active_count)
            active_oldest_age = now - active_oldest if active_count else 0
            self.metrics.wal_oldest_file_age_seconds.set(active_oldest_age)

            # CE3-06: WAL SLO metrics — these gate replay alerts. Active tier
            # is the right choice (loader processes from there).
            self.metrics.wal_backlog_files.set(active_count)
            self.metrics.wal_replay_lag_seconds.set(active_oldest_age)
            now_ts = now
            dt = max(1e-6, now_ts - self._eta_sample_last_ts)
            delta_files = max(0, self._processed_files_total - self._eta_sample_last_processed)
            files_per_s = (delta_files / dt) if delta_files > 0 else 0.0
            if active_count <= 0:
                eta_s = 0.0
            elif files_per_s > 0:
                eta_s = active_count / files_per_s
            else:
                eta_s = 0.0
            self.metrics.wal_drain_eta_seconds.set(float(eta_s))
            self._eta_sample_last_ts = now_ts
            self._eta_sample_last_processed = self._processed_files_total

        # Log warnings: now sum of all tiers so the operator sees the real
        # disk pressure (the live incident showed 0 active but 3 GB on disk).
        total_bytes = active_size + archive_size + dlq_size
        total_count = active_count + archive_count + dlq_count
        size_mb = total_bytes / (1024 * 1024)
        if size_mb > self._wal_size_critical_mb:
            logger.critical(
                "WAL directory critically large",
                size_mb=round(size_mb, 2),
                file_count=total_count,
                active_mb=round(active_size / (1024 * 1024), 2),
                archive_mb=round(archive_size / (1024 * 1024), 2),
                dlq_mb=round(dlq_size / (1024 * 1024), 2),
                threshold_mb=self._wal_size_critical_mb,
            )
        elif size_mb > self._wal_size_warning_mb:
            logger.warning(
                "WAL directory large",
                size_mb=round(size_mb, 2),
                file_count=total_count,
                active_mb=round(active_size / (1024 * 1024), 2),
                archive_mb=round(archive_size / (1024 * 1024), 2),
                dlq_mb=round(dlq_size / (1024 * 1024), 2),
                threshold_mb=self._wal_size_warning_mb,
            )
    except Exception as e:
        logger.warning("WAL accumulation check failed", error=str(e))
