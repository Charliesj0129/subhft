"""WAL Loader Service — loads WAL files into ClickHouse.

This module is the public entry point.  Heavy logic has been extracted into
private helper modules:

- ``_loader_common`` — shared constants, JSON codec, price scaling
- ``_loader_ch``     — ClickHouse connection, insert-with-retry, dedup
- ``_loader_wal``    — WAL file discovery, manifest, single-file processing
- ``_loader_dlq``    — DLQ write/replay, cleanup, WAL accumulation monitoring
- ``_loader_batch``  — per-table row formatting for ClickHouse inserts
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from hft_platform.recorder.wal_scheduler import WALScheduler

from hft_platform.core import timebase
from hft_platform.recorder import _loader_batch as _batch
from hft_platform.recorder import _loader_ch as _ch
from hft_platform.recorder import _loader_dlq as _dlq
from hft_platform.recorder import _loader_wal as _wal
from hft_platform.recorder._loader_common import (
    DEFAULT_INSERT_BASE_DELAY_S,
    DEFAULT_INSERT_MAX_BACKOFF_S,
    DEFAULT_INSERT_MAX_RETRIES,
    logger,
)


class WALLoaderService:
    """Loads WAL files into ClickHouse with retry, DLQ, and dedup support."""

    # Configurable poll interval (default 1s, was 5s)
    DEFAULT_POLL_INTERVAL_S = 1.0
    # Connection retry configuration
    DEFAULT_CONNECT_MAX_RETRIES = 10
    DEFAULT_CONNECT_BASE_DELAY_S = 5.0
    DEFAULT_CONNECT_MAX_BACKOFF_S = 300.0  # 5 minutes max between retries

    def __init__(
        self,
        wal_dir: str = ".wal",
        archive_dir: str = ".wal/archive",
        ch_host: str = "clickhouse",
        ch_port: int = 9000,
    ) -> None:
        self.wal_dir = wal_dir
        self.archive_dir = archive_dir
        self.running = False
        self.poll_interval_s = float(os.getenv("HFT_WAL_POLL_INTERVAL_S", str(self.DEFAULT_POLL_INTERVAL_S)))

        # ClickHouse Client (default to native protocol port 9000)
        self.ch_host = os.getenv("HFT_CLICKHOUSE_HOST") or os.getenv("CLICKHOUSE_HOST") or ch_host
        self.ch_port = int(os.getenv("HFT_CLICKHOUSE_PORT") or os.getenv("CLICKHOUSE_PORT") or ch_port)
        self.ch_client = None
        self._ch_lock = threading.Lock()

        # Connection retry configuration with circuit breaker pattern
        self._connect_max_retries = int(
            os.getenv(
                "HFT_CONNECT_MAX_RETRIES",
                str(self.DEFAULT_CONNECT_MAX_RETRIES),
            )
        )
        self._connect_base_delay_s = float(
            os.getenv(
                "HFT_CONNECT_BASE_DELAY_S",
                str(self.DEFAULT_CONNECT_BASE_DELAY_S),
            )
        )
        self._connect_max_backoff_s = float(
            os.getenv(
                "HFT_CONNECT_MAX_BACKOFF_S",
                str(self.DEFAULT_CONNECT_MAX_BACKOFF_S),
            )
        )
        self._connect_failures = 0
        self._circuit_open_until = 0.0

        # Insert retry configuration
        self._insert_max_retries = int(os.getenv("HFT_INSERT_MAX_RETRIES", str(DEFAULT_INSERT_MAX_RETRIES)))
        self._insert_base_delay_s = float(os.getenv("HFT_INSERT_BASE_DELAY_S", str(DEFAULT_INSERT_BASE_DELAY_S)))
        self._insert_max_backoff_s = float(os.getenv("HFT_INSERT_MAX_BACKOFF_S", str(DEFAULT_INSERT_MAX_BACKOFF_S)))

        # Dead Letter Queue directory for failed inserts
        self.dlq_dir = os.path.join(self.wal_dir, "dlq")
        # Quarantine directory for corrupt files
        self.corrupt_dir = os.path.join(self.wal_dir, "corrupt")

        # DLQ cleanup configuration (B3)
        self._dlq_retention_days = int(os.getenv("HFT_DLQ_RETENTION_DAYS", "7"))
        self._dlq_archive_path = os.getenv("HFT_DLQ_ARCHIVE_PATH") or None
        self._last_dlq_cleanup_ts = 0.0
        self._dlq_cleanup_interval_s = 3600.0  # 1 hour

        # Corrupt file cleanup configuration (B5)
        self._corrupt_retention_days = int(os.getenv("HFT_CORRUPT_RETENTION_DAYS", "30"))
        self._last_corrupt_cleanup_ts = 0.0

        # Archive cleanup configuration (long-run stability)
        self._archive_retention_days = int(os.getenv("HFT_ARCHIVE_RETENTION_DAYS", "14"))
        self._last_archive_cleanup_ts = 0.0

        # WAL accumulation monitoring (C5)
        self._wal_size_warning_mb = float(os.getenv("HFT_WAL_SIZE_WARNING_MB", "100"))
        self._wal_size_critical_mb = float(os.getenv("HFT_WAL_SIZE_CRITICAL_MB", "500"))
        self._last_wal_check_ts = 0.0
        self._wal_check_interval_s = 60.0  # Check every minute
        self._processed_files_total = 0
        self._eta_sample_last_ts = timebase.now_s()
        self._eta_sample_last_processed = 0
        self.metrics = None  # Will be set when run() is called
        self._wal_scheduler: WALScheduler | None = None

        # P0-4: Async mode configuration
        self._async_enabled = os.getenv("HFT_LOADER_ASYNC", "1").lower() not in {"0", "false", "no", "off"}

        # P1-1: WAL manifest tracking
        self._manifest_enabled = os.getenv("HFT_WAL_USE_MANIFEST", "1").lower() not in {"0", "false", "no", "off"}
        self._manifest_path = os.getenv("HFT_WAL_MANIFEST_PATH", os.path.join(wal_dir, "manifest.txt"))
        self._manifest: set[str] = set()
        self._manifest_lock = threading.Lock()

        # CC-3: Parallel WAL file processing
        self._loader_concurrency = int(os.getenv("HFT_WAL_LOADER_CONCURRENCY", "4"))

        # EC-1: WAL replay dedup guard
        self._dedup_enabled = os.getenv("HFT_WAL_DEDUP_ENABLED", "1").lower() in {"1", "true", "yes", "on"}

        # EC-2: WAL file timestamp ordering
        self._strict_order = os.getenv("HFT_WAL_STRICT_ORDER", "0").lower() in {"1", "true", "yes", "on"}
        self._last_processed_ts: int = 0

        # CE3-03: Shard claim registry
        from hft_platform.recorder.shard_claim import FileClaimRegistry

        self._claim_registry = FileClaimRegistry(
            claim_dir=os.path.join(wal_dir, "claims"),
        )

    # ------------------------------------------------------------------
    # ClickHouse connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        _ch.connect(self)

    async def _connect_async(self) -> None:
        """Async connect - does not block the event loop."""
        await asyncio.to_thread(self.connect)

    def _compute_connect_backoff(self, attempt: int) -> float:
        return _ch.compute_connect_backoff(self, attempt)

    # ------------------------------------------------------------------
    # Main loop (sync)
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.running = True
        if not os.path.exists(self.archive_dir):
            os.makedirs(self.archive_dir)

        self._init_run_common()

        logger.info("Starting WAL Loader", wal_dir=self.wal_dir)
        self._ensure_scheduler()

        try:
            while self.running:
                if not self.ch_client:
                    if not self._handle_reconnect_sync():
                        continue

                try:
                    self.process_files()
                except ConnectionError as e:
                    logger.error(
                        "Connection error during file processing",
                        error=str(e),
                        error_type="ConnectionError",
                    )
                    self.ch_client = None
                except TimeoutError as e:
                    logger.error(
                        "Timeout during file processing",
                        error=str(e),
                        error_type="TimeoutError",
                    )
                except OSError as e:
                    logger.error(
                        "OS error during file processing",
                        error=str(e),
                        error_type="OSError",
                        errno=e.errno,
                    )
                except Exception as e:
                    logger.error(
                        "Unexpected error processing files",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                self._run_cleanup_sync()
                time.sleep(self.poll_interval_s)
        finally:
            if self._wal_scheduler and self._wal_scheduler.running:
                self._wal_scheduler.stop()

    # ------------------------------------------------------------------
    # Main loop (async)
    # ------------------------------------------------------------------

    async def run_async(self) -> None:
        """Async main loop - non-blocking (P0-4)."""
        self.running = True
        if not os.path.exists(self.archive_dir):
            os.makedirs(self.archive_dir)

        self._init_run_common()

        logger.info("Starting WAL Loader (async mode)", wal_dir=self.wal_dir)
        self._ensure_scheduler()

        try:
            while self.running:
                if not self.ch_client:
                    if not await self._handle_reconnect_async():
                        continue

                try:
                    await asyncio.to_thread(self.process_files)
                except ConnectionError as e:
                    logger.error(
                        "Connection error during file processing",
                        error=str(e),
                        error_type="ConnectionError",
                    )
                    self.ch_client = None
                except TimeoutError as e:
                    logger.error(
                        "Timeout during file processing",
                        error=str(e),
                        error_type="TimeoutError",
                    )
                except OSError as e:
                    logger.error(
                        "OS error during file processing",
                        error=str(e),
                        error_type="OSError",
                        errno=e.errno,
                    )
                except Exception as e:
                    logger.error(
                        "Unexpected error processing files",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                try:
                    await asyncio.to_thread(self._cleanup_old_dlq_files)
                    await asyncio.to_thread(self._cleanup_old_corrupt_files)
                    await asyncio.to_thread(self._cleanup_old_archive_files)
                except Exception as e:
                    logger.warning("Cleanup task failed", error=str(e))

                try:
                    await asyncio.to_thread(self._check_wal_accumulation)
                except Exception as e:
                    logger.warning("WAL accumulation check failed", error=str(e))

                await asyncio.sleep(self.poll_interval_s)
        finally:
            if self._wal_scheduler and self._wal_scheduler.running:
                self._wal_scheduler.stop()

    # ------------------------------------------------------------------
    # Shared initialisation for run / run_async
    # ------------------------------------------------------------------

    def _init_run_common(self) -> None:
        """Shared setup for both sync and async run loops."""
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            self.metrics = MetricsRegistry.get()
        except Exception as exc:
            logger.warning("Failed to initialize metrics", error=str(exc))
            self.metrics = None

        if self._manifest_enabled:
            self._load_manifest()

        try:
            self._claim_registry.recover_stale_claims()
        except Exception as exc:
            logger.warning("Stale claim recovery failed", error=str(exc))

        from hft_platform.recorder.replay_contract import (
            validate_replay_preconditions,
        )

        violations = validate_replay_preconditions(self)
        if violations:
            for v in violations:
                logger.warning("ReplayContract violation", violation=v)

    def _ensure_scheduler(self) -> None:
        if self._wal_scheduler is None:
            try:
                from hft_platform.recorder.wal_scheduler import (
                    WALScheduler,
                )

                self._wal_scheduler = WALScheduler(self)
                self._wal_scheduler.start()
            except Exception as exc:
                logger.warning("Failed to start WAL scheduler", error=str(exc))

    # ------------------------------------------------------------------
    # Reconnect helpers
    # ------------------------------------------------------------------

    def _handle_reconnect_sync(self) -> bool:
        """Handle ClickHouse reconnection with circuit breaker.

        Returns True if connected.
        """
        now = timebase.now_s()
        if self._circuit_open_until > now:
            sleep_time = min(self._circuit_open_until - now, 60.0)
            logger.debug(
                "Connection circuit breaker open, waiting",
                sleep_s=round(sleep_time, 1),
                failures=self._connect_failures,
            )
            time.sleep(sleep_time)
            return False

        self.connect()
        return self._process_connect_result_sync()

    async def _handle_reconnect_async(self) -> bool:
        """Async reconnection with circuit breaker.

        Returns True if connected.
        """
        now = timebase.now_s()
        if self._circuit_open_until > now:
            sleep_time = min(self._circuit_open_until - now, 60.0)
            logger.debug(
                "Connection circuit breaker open, waiting",
                sleep_s=round(sleep_time, 1),
                failures=self._connect_failures,
            )
            await asyncio.sleep(sleep_time)
            return False

        await self._connect_async()
        return await self._process_connect_result_async()

    def _process_connect_result_sync(self) -> bool:
        if not self.ch_client:
            self._connect_failures += 1
            if self._connect_failures >= self._connect_max_retries:
                backoff = self._compute_connect_backoff(self._connect_failures - self._connect_max_retries)
                self._circuit_open_until = timebase.now_s() + backoff
                logger.error(
                    "ClickHouse connection failed repeatedly, circuit breaker opened",
                    failures=self._connect_failures,
                    backoff_s=round(backoff, 1),
                )
            else:
                delay = self._compute_connect_backoff(self._connect_failures)
                logger.warning(
                    "ClickHouse connection failed, retrying with backoff",
                    attempt=self._connect_failures,
                    max_retries=self._connect_max_retries,
                    delay_s=round(delay, 1),
                )
                time.sleep(delay)
            return False
        self._connect_failures = 0
        self._circuit_open_until = 0.0
        return True

    async def _process_connect_result_async(self) -> bool:
        if not self.ch_client:
            self._connect_failures += 1
            if self._connect_failures >= self._connect_max_retries:
                backoff = self._compute_connect_backoff(self._connect_failures - self._connect_max_retries)
                self._circuit_open_until = timebase.now_s() + backoff
                logger.error(
                    "ClickHouse connection failed repeatedly, circuit breaker opened",
                    failures=self._connect_failures,
                    backoff_s=round(backoff, 1),
                )
            else:
                delay = self._compute_connect_backoff(self._connect_failures)
                logger.warning(
                    "ClickHouse connection failed, retrying with backoff",
                    attempt=self._connect_failures,
                    max_retries=self._connect_max_retries,
                    delay_s=round(delay, 1),
                )
                await asyncio.sleep(delay)
            return False
        self._connect_failures = 0
        self._circuit_open_until = 0.0
        return True

    # ------------------------------------------------------------------
    # Sync cleanup helper
    # ------------------------------------------------------------------

    def _run_cleanup_sync(self) -> None:
        try:
            self._cleanup_old_dlq_files()
            self._cleanup_old_corrupt_files()
            self._cleanup_old_archive_files()
        except Exception as e:
            logger.warning("Cleanup task failed", error=str(e))

        try:
            self._check_wal_accumulation()
        except Exception as e:
            logger.warning("WAL accumulation check failed", error=str(e))

    # ------------------------------------------------------------------
    # Manifest delegates
    # ------------------------------------------------------------------

    def _load_manifest(self) -> None:
        _wal.load_manifest(self)

    def _save_manifest(self) -> None:
        _wal.save_manifest(self)

    @staticmethod
    def _extract_file_ts(fname: str) -> int:
        return _wal.extract_file_ts(fname)

    def _get_new_files(self) -> list[str]:
        return _wal.get_new_files(self)

    def _mark_processed(self, filename: str) -> None:
        _wal.mark_processed(self, filename)

    # ------------------------------------------------------------------
    # Table name parsing delegates
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_table_from_filename(fname: str) -> str:
        return _wal.parse_table_from_filename(fname)

    @staticmethod
    def _parse_batch_table_name(table_name: str) -> str:
        return _wal.parse_batch_table_name(table_name)

    # ------------------------------------------------------------------
    # File processing delegates
    # ------------------------------------------------------------------

    def _process_single_file(self, fpath: str, force: bool = False) -> bool:
        return _wal.process_single_file(self, fpath, force)

    def _process_single_file_inner(self, fpath: str, fname: str, force: bool) -> bool:
        return _wal._process_single_file_inner(self, fpath, fname, force)

    def _insert_with_dedup(self, target_table: str, rows: list, fname: str) -> bool:
        return _ch.insert_with_dedup(self, target_table, rows, fname)

    def process_files(self, force: bool = False) -> None:
        _wal.process_files(self, force)

    # ------------------------------------------------------------------
    # DLQ delegates
    # ------------------------------------------------------------------

    def _quarantine_corrupt_file(self, fpath: str, fname: str, reason: str) -> None:
        _dlq.quarantine_corrupt_file(self, fpath, fname, reason)

    def _cleanup_old_dlq_files(self) -> None:
        _dlq.cleanup_old_dlq_files(self)

    def _cleanup_old_corrupt_files(self) -> None:
        _dlq.cleanup_old_corrupt_files(self)

    def _cleanup_old_archive_files(self) -> None:
        _dlq.cleanup_old_archive_files(self)

    def _check_wal_accumulation(self) -> None:
        _dlq.check_wal_accumulation(self)

    def _write_to_dlq(self, table: str, rows: List[Dict[str, Any]], error: str) -> None:
        _dlq.write_to_dlq(self, table, rows, error)

    def replay_dlq(self, dry_run: bool = False, max_files: int | None = None) -> dict:
        return _dlq.replay_dlq(self, dry_run, max_files)

    # ------------------------------------------------------------------
    # Insert delegates
    # ------------------------------------------------------------------

    def _compute_insert_backoff(self, attempt: int) -> float:
        return _ch.compute_insert_backoff(self, attempt)

    def insert_batch(self, table: str, rows: List[Dict[str, Any]]) -> bool:
        """Insert batch with retry logic. Returns True on success."""
        return _batch.insert_batch_for_table(self, table, rows)

    def _insert_with_retry(
        self,
        full_table_name: str,
        cols: list,
        data: list,
        table_alias: str,
        row_count: int,
    ) -> bool:
        return _ch.insert_with_retry(self, full_table_name, cols, data, table_alias, row_count)

    def _is_duplicate(self, table: str, content_hash: str) -> bool:
        return _ch.is_duplicate(self, table, content_hash)

    def _record_dedup(self, table: str, content_hash: str, row_count: int) -> None:
        _ch.record_dedup(self, table, content_hash, row_count)


if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging

    configure_logging()
    loader = WALLoaderService()
    if loader._async_enabled:
        asyncio.run(loader.run_async())
    else:
        loader.run()
