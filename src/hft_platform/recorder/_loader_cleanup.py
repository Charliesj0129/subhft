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


def _check_wal_accumulation(self: WALLoaderService) -> None:
    """Check WAL directory size and emit metrics (C5)."""
    now = timebase.now_s()
    if now - self._last_wal_check_ts < self._wal_check_interval_s:
        return
    self._last_wal_check_ts = now

    if not os.path.isdir(self.wal_dir):
        return

    total_size = 0
    file_count = 0
    oldest_mtime = now

    try:
        for fname in os.listdir(self.wal_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(self.wal_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                stat = os.stat(fpath)
                total_size += stat.st_size
                file_count += 1
                oldest_mtime = min(oldest_mtime, stat.st_mtime)
            except OSError:
                continue

        # Emit metrics
        if self.metrics:
            self.metrics.wal_directory_size_bytes.set(total_size)
            self.metrics.wal_file_count.set(file_count)
            oldest_age = now - oldest_mtime if file_count else 0
            self.metrics.wal_oldest_file_age_seconds.set(oldest_age)
            # CE3-06: WAL SLO metrics
            self.metrics.wal_backlog_files.set(file_count)
            self.metrics.wal_replay_lag_seconds.set(oldest_age)
            now_ts = now
            dt = max(1e-6, now_ts - self._eta_sample_last_ts)
            delta_files = max(0, self._processed_files_total - self._eta_sample_last_processed)
            files_per_s = (delta_files / dt) if delta_files > 0 else 0.0
            if file_count <= 0:
                eta_s = 0.0
            elif files_per_s > 0:
                eta_s = file_count / files_per_s
            else:
                eta_s = 0.0
            self.metrics.wal_drain_eta_seconds.set(float(eta_s))
            self._eta_sample_last_ts = now_ts
            self._eta_sample_last_processed = self._processed_files_total

        # Log warnings
        size_mb = total_size / (1024 * 1024)
        if size_mb > self._wal_size_critical_mb:
            logger.critical(
                "WAL directory critically large",
                size_mb=round(size_mb, 2),
                file_count=file_count,
                threshold_mb=self._wal_size_critical_mb,
            )
        elif size_mb > self._wal_size_warning_mb:
            logger.warning(
                "WAL directory large",
                size_mb=round(size_mb, 2),
                file_count=file_count,
                threshold_mb=self._wal_size_warning_mb,
            )
    except Exception as e:
        logger.warning("WAL accumulation check failed", error=str(e))
