import asyncio
import fcntl
import glob
import os
import shutil
import threading
import time
from typing import Any, Dict, List

try:
    import orjson

    def _dumps(obj: object) -> str:
        return orjson.dumps(obj).decode()

    _loads = orjson.loads
except ImportError:
    import json

    _dumps = json.dumps  # type: ignore[assignment]
    _loads = json.loads

import clickhouse_connect
from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.recorder.schema import apply_schema, ensure_price_scaled_views

logger = get_logger("wal_loader")

# Default retry configuration for batch inserts
DEFAULT_INSERT_MAX_RETRIES = 3
DEFAULT_INSERT_BASE_DELAY_S = 0.5
DEFAULT_INSERT_MAX_BACKOFF_S = 5.0
try:
    _TS_MAX_FUTURE_NS = int(float(os.getenv("HFT_TS_MAX_FUTURE_S", "5")) * 1e9)
except Exception as e:
    logger.warning(
        "Failed to parse HFT_TS_MAX_FUTURE_S, timestamp validation disabled",
        error=str(e),
        env_value=os.getenv("HFT_TS_MAX_FUTURE_S"),
    )
    _TS_MAX_FUTURE_NS = 0


class WALLoaderService:
    # Configurable poll interval (default 1s, was 5s)
    DEFAULT_POLL_INTERVAL_S = 1.0
    # Connection retry configuration
    DEFAULT_CONNECT_MAX_RETRIES = 10
    DEFAULT_CONNECT_BASE_DELAY_S = 5.0
    DEFAULT_CONNECT_MAX_BACKOFF_S = 300.0  # 5 minutes max between retries

    def __init__(self, wal_dir=".wal", archive_dir=".wal/archive", ch_host="clickhouse", ch_port=9000):
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
        self._connect_max_retries = int(os.getenv("HFT_CONNECT_MAX_RETRIES", str(self.DEFAULT_CONNECT_MAX_RETRIES)))
        self._connect_base_delay_s = float(
            os.getenv("HFT_CONNECT_BASE_DELAY_S", str(self.DEFAULT_CONNECT_BASE_DELAY_S))
        )
        self._connect_max_backoff_s = float(
            os.getenv("HFT_CONNECT_MAX_BACKOFF_S", str(self.DEFAULT_CONNECT_MAX_BACKOFF_S))
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

        # WAL accumulation monitoring (C5)
        self._wal_size_warning_mb = float(os.getenv("HFT_WAL_SIZE_WARNING_MB", "100"))
        self._wal_size_critical_mb = float(os.getenv("HFT_WAL_SIZE_CRITICAL_MB", "500"))
        self._last_wal_check_ts = 0.0
        self._wal_check_interval_s = 60.0  # Check every minute
        self.metrics = None  # Will be set when run() is called
        self._wal_scheduler = None

        # P0-4: Async mode configuration
        self._async_enabled = os.getenv("HFT_LOADER_ASYNC", "1").lower() not in {"0", "false", "no", "off"}

        # P1-1: WAL manifest tracking
        self._manifest_enabled = os.getenv("HFT_WAL_USE_MANIFEST", "1").lower() not in {"0", "false", "no", "off"}
        self._manifest_path = os.getenv("HFT_WAL_MANIFEST_PATH", os.path.join(wal_dir, "manifest.txt"))
        self._manifest: set[str] = set()
        import threading as _threading

        self._manifest_lock = _threading.Lock()

        # CC-3: Parallel WAL file processing
        self._loader_concurrency = int(os.getenv("HFT_WAL_LOADER_CONCURRENCY", "4"))

        # EC-1: WAL replay dedup guard
        self._dedup_enabled = os.getenv("HFT_WAL_DEDUP_ENABLED", "0").lower() in {"1", "true", "yes", "on"}

        # EC-2: WAL file timestamp ordering
        self._strict_order = os.getenv("HFT_WAL_STRICT_ORDER", "0").lower() in {"1", "true", "yes", "on"}
        self._last_processed_ts: int = 0

        # CE3-03: Shard claim registry
        from hft_platform.recorder.shard_claim import FileClaimRegistry

        self._claim_registry = FileClaimRegistry(
            claim_dir=os.path.join(wal_dir, "claims"),
        )

    def connect(self):
        try:
            ch_username = (
                os.getenv("HFT_CLICKHOUSE_USER")
                or os.getenv("HFT_CLICKHOUSE_USERNAME")
                or os.getenv("CLICKHOUSE_USER")
                or os.getenv("CLICKHOUSE_USERNAME")
                or "default"
            )
            ch_password = os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD") or ""
            self.ch_client = clickhouse_connect.get_client(
                host=self.ch_host, port=self.ch_port, username=ch_username, password=ch_password
            )
            # Ensure schema exists (rudimentary check or run init sql)
            try:
                apply_schema(self.ch_client)
            except Exception as e:
                logger.error("Schema initialization failed", error=str(e))
            try:
                ensure_price_scaled_views(self.ch_client)
            except Exception as e:
                logger.error("Schema view repair failed", error=str(e))
            logger.info("Connected to ClickHouse and ensured schema.")
        except ConnectionError as e:
            logger.error("Connection refused by ClickHouse", error=str(e), host=self.ch_host, port=self.ch_port)
            self.ch_client = None
        except TimeoutError as e:
            logger.error("Connection timeout to ClickHouse", error=str(e), host=self.ch_host, port=self.ch_port)
            self.ch_client = None
        except FileNotFoundError as e:
            logger.error("Schema file not found", error=str(e))
            # Still connected, just no schema init
        except Exception as e:
            logger.error("Failed to connect to ClickHouse", error=str(e), error_type=type(e).__name__)
            self.ch_client = None

    async def _connect_async(self):
        """Async connect - does not block the event loop."""
        await asyncio.to_thread(self.connect)

    def _compute_connect_backoff(self, attempt: int) -> float:
        """Compute exponential backoff delay for connection retry."""
        import random

        delay = min(self._connect_base_delay_s * (2**attempt), self._connect_max_backoff_s)
        jitter = delay * 0.25 * (random.random() * 2 - 1)
        return max(1.0, delay + jitter)

    def run(self):
        self.running = True
        if not os.path.exists(self.archive_dir):
            os.makedirs(self.archive_dir)

        # Initialize metrics
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            self.metrics = MetricsRegistry.get()
        except Exception as exc:
            logger.warning("Failed to initialize metrics", error=str(exc))
            self.metrics = None

        # Load manifest for P1-1
        if self._manifest_enabled:
            self._load_manifest()

        # CE3-03: recover stale claims on startup
        try:
            self._claim_registry.recover_stale_claims()
        except Exception as exc:
            logger.warning("Stale claim recovery failed", error=str(exc))

        # CE3-04: validate replay preconditions
        from hft_platform.recorder.replay_contract import validate_replay_preconditions

        violations = validate_replay_preconditions(self)
        if violations:
            for v in violations:
                logger.warning("ReplayContract violation", violation=v)

        logger.info("Starting WAL Loader", wal_dir=self.wal_dir)
        if self._wal_scheduler is None:
            try:
                from hft_platform.recorder.wal_scheduler import WALScheduler

                self._wal_scheduler = WALScheduler(self)
                self._wal_scheduler.start()
            except Exception as exc:
                logger.warning("Failed to start WAL scheduler", error=str(exc))

        try:
            while self.running:
                if not self.ch_client:
                    # Check circuit breaker
                    now = timebase.now_s()
                    if self._circuit_open_until > now:
                        sleep_time = min(self._circuit_open_until - now, 60.0)
                        logger.debug(
                            "Connection circuit breaker open, waiting",
                            sleep_s=round(sleep_time, 1),
                            failures=self._connect_failures,
                        )
                        time.sleep(sleep_time)
                        continue

                    self.connect()
                    if not self.ch_client:
                        self._connect_failures += 1
                        if self._connect_failures >= self._connect_max_retries:
                            # Open circuit breaker
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
                        continue
                    else:
                        # Reset on successful connection
                        self._connect_failures = 0
                        self._circuit_open_until = 0.0

                try:
                    self.process_files()
                except ConnectionError as e:
                    logger.error("Connection error during file processing", error=str(e), error_type="ConnectionError")
                    # Reset client to force reconnect
                    self.ch_client = None
                except TimeoutError as e:
                    logger.error("Timeout during file processing", error=str(e), error_type="TimeoutError")
                except OSError as e:
                    logger.error("OS error during file processing", error=str(e), error_type="OSError", errno=e.errno)
                except Exception as e:
                    logger.error(
                        "Unexpected error processing files",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                # Cleanup old DLQ and corrupt files (B3, B5)
                try:
                    self._cleanup_old_dlq_files()
                    self._cleanup_old_corrupt_files()
                except Exception as e:
                    logger.warning("Cleanup task failed", error=str(e))

                # WAL accumulation monitoring (C5)
                try:
                    self._check_wal_accumulation()
                except Exception as e:
                    logger.warning("WAL accumulation check failed", error=str(e))

                time.sleep(self.poll_interval_s)
        finally:
            if self._wal_scheduler and self._wal_scheduler.running:
                self._wal_scheduler.stop()

    async def run_async(self) -> None:
        """Async main loop - non-blocking (P0-4).

        This method is the async equivalent of run(), using asyncio.sleep
        instead of blocking time.sleep to comply with the Async Law.
        """
        self.running = True
        if not os.path.exists(self.archive_dir):
            os.makedirs(self.archive_dir)

        # Initialize metrics
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            self.metrics = MetricsRegistry.get()
        except Exception as exc:
            logger.warning("Failed to initialize metrics", error=str(exc))
            self.metrics = None

        # Load manifest for P1-1
        if self._manifest_enabled:
            self._load_manifest()

        # CE3-03: recover stale claims on startup
        try:
            self._claim_registry.recover_stale_claims()
        except Exception as exc:
            logger.warning("Stale claim recovery failed", error=str(exc))

        # CE3-04: validate replay preconditions
        from hft_platform.recorder.replay_contract import validate_replay_preconditions

        violations = validate_replay_preconditions(self)
        if violations:
            for v in violations:
                logger.warning("ReplayContract violation", violation=v)

        logger.info("Starting WAL Loader (async mode)", wal_dir=self.wal_dir)
        if self._wal_scheduler is None:
            try:
                from hft_platform.recorder.wal_scheduler import WALScheduler

                self._wal_scheduler = WALScheduler(self)
                self._wal_scheduler.start()
            except Exception as exc:
                logger.warning("Failed to start WAL scheduler", error=str(exc))

        try:
            while self.running:
                if not self.ch_client:
                    # Check circuit breaker
                    now = timebase.now_s()
                    if self._circuit_open_until > now:
                        sleep_time = min(self._circuit_open_until - now, 60.0)
                        logger.debug(
                            "Connection circuit breaker open, waiting",
                            sleep_s=round(sleep_time, 1),
                            failures=self._connect_failures,
                        )
                        await asyncio.sleep(sleep_time)
                        continue

                    await self._connect_async()
                    if not self.ch_client:
                        self._connect_failures += 1
                        if self._connect_failures >= self._connect_max_retries:
                            # Open circuit breaker
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
                        continue
                    else:
                        # Reset on successful connection
                        self._connect_failures = 0
                        self._circuit_open_until = 0.0

                try:
                    # Offload blocking file processing to thread pool
                    await asyncio.to_thread(self.process_files)
                except ConnectionError as e:
                    logger.error("Connection error during file processing", error=str(e), error_type="ConnectionError")
                    self.ch_client = None
                except TimeoutError as e:
                    logger.error("Timeout during file processing", error=str(e), error_type="TimeoutError")
                except OSError as e:
                    logger.error("OS error during file processing", error=str(e), error_type="OSError", errno=e.errno)
                except Exception as e:
                    logger.error(
                        "Unexpected error processing files",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                # Cleanup old DLQ and corrupt files (B3, B5)
                try:
                    await asyncio.to_thread(self._cleanup_old_dlq_files)
                    await asyncio.to_thread(self._cleanup_old_corrupt_files)
                except Exception as e:
                    logger.warning("Cleanup task failed", error=str(e))

                # WAL accumulation monitoring (C5)
                try:
                    await asyncio.to_thread(self._check_wal_accumulation)
                except Exception as e:
                    logger.warning("WAL accumulation check failed", error=str(e))

                # Non-blocking sleep (P0-4)
                await asyncio.sleep(self.poll_interval_s)
        finally:
            if self._wal_scheduler and self._wal_scheduler.running:
                self._wal_scheduler.stop()

    def _load_manifest(self) -> None:
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

    def _save_manifest(self) -> None:
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
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.warning("Failed to save manifest", error=str(e))

    @staticmethod
    def _extract_file_ts(fname: str) -> int:
        """Extract nanosecond timestamp from WAL filename (EC-2).

        Filename format: {table}_{nanosecond_ts}.jsonl
        Returns 0 if parsing fails.
        """
        try:
            base = fname.rsplit(".", 1)[0]  # strip .jsonl
            ts_str = base.rsplit("_", 1)[-1]
            return int(ts_str)
        except (ValueError, IndexError):
            return 0

    def _get_new_files(self) -> list[str]:
        """Get list of new WAL files not in manifest (P1-1, EC-2).

        Returns:
            List of full file paths for new files to process,
            sorted by embedded nanosecond timestamp.
        """
        if not self._manifest_enabled:
            files = glob.glob(os.path.join(self.wal_dir, "*.jsonl"))
            files.sort(key=lambda p: self._extract_file_ts(os.path.basename(p)))
            return files

        try:
            current = {f for f in os.listdir(self.wal_dir) if f.endswith(".jsonl")}
        except OSError:
            return []

        new_files = sorted(current - self._manifest, key=self._extract_file_ts)
        return [os.path.join(self.wal_dir, f) for f in new_files]

    def _mark_processed(self, filename: str) -> None:
        """Mark file as processed in manifest (P1-1, CC-3 thread-safe)."""
        if not self._manifest_enabled:
            return
        fname = os.path.basename(filename)
        with self._manifest_lock:
            self._manifest.add(fname)

    @staticmethod
    def _parse_table_from_filename(fname: str) -> str:
        """Extract target table name from WAL filename."""
        base = fname
        if "_" in fname:
            base = "_".join(fname.split("_")[:-1])
        if base.startswith("hft."):
            base = base.split(".", 1)[1]
        if base.startswith("market_data"):
            return "market_data"
        if base.startswith("orders"):
            return "orders"
        if base.startswith("fills"):
            return "trades"  # Mapping 'fills' topic to 'trades' table
        if base.startswith("risk_log"):
            return "risk_log"
        if base.startswith("backtest_runs"):
            return "backtest_runs"
        if base.startswith("latency_spans"):
            return "latency_spans"
        return base or "unknown"

    def _process_single_file(self, fpath: str, force: bool = False) -> bool:
        """Process a single WAL file (CC-3: extracted for parallel use).

        Supports both single-table files (table_ts.jsonl) and multi-table
        batch files (batch_ts.jsonl) from WALBatchWriter (CC-4).

        Returns True if the file was successfully processed and archived.
        """
        fname = os.path.basename(fpath)

        # CE3-03: Shard claim — skip if another worker has it
        if not self._claim_registry.try_claim(fname):
            logger.debug("WAL file already claimed, skipping", file=fname)
            return False

        try:
            return self._process_single_file_inner(fpath, fname, force)
        finally:
            self._claim_registry.release_claim(fname)

    def _process_single_file_inner(self, fpath: str, fname: str, force: bool) -> bool:
        """Inner processing logic (called after claim acquired)."""
        # Check modification time to ensure writer is done
        if not force:
            try:
                mtime = os.path.getmtime(fpath)
                if timebase.now_s() - mtime < 2.0:
                    return False
            except OSError:
                return False

        # EC-2: Strict ordering check
        if self._strict_order:
            file_ts = self._extract_file_ts(fname)
            if file_ts and file_ts < self._last_processed_ts:
                logger.warning(
                    "WAL file timestamp out of order, skipping (strict mode)",
                    file=fname,
                    file_ts=file_ts,
                    last_ts=self._last_processed_ts,
                )
                return False

        logger.info("Loading file", file=fname)

        # Read all lines from file
        all_lines: list = []
        corrupt_lines = 0
        try:
            with open(fpath, "r") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    logger.debug("File locked by writer, skipping", file=fname)
                    return False
                try:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            all_lines.append(_loads(line))
                        except Exception:
                            corrupt_lines += 1
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            if corrupt_lines > 0 and not all_lines:
                self._quarantine_corrupt_file(fpath, fname, f"All {corrupt_lines} lines corrupt")
                return False
            elif corrupt_lines > 0:
                logger.warning(
                    "Partial corruption in WAL file",
                    file=fname,
                    corrupt_lines=corrupt_lines,
                    valid_rows=len(all_lines),
                )

        except FileNotFoundError:
            return False

        if not all_lines:
            # Empty file, archive it
            try:
                shutil.move(fpath, os.path.join(self.archive_dir, fname))
            except FileNotFoundError:
                pass
            return True

        # CC-4: Detect multi-table batch format
        # Check if first line is a batch header: {"__wal_table__": ..., "__row_count__": ...}
        is_batch = isinstance(all_lines[0], dict) and "__wal_table__" in all_lines[0]

        if is_batch:
            # Parse multi-table sections
            table_batches: list[tuple[str, list]] = []
            current_table = None
            current_rows: list = []

            for obj in all_lines:
                if isinstance(obj, dict) and "__wal_table__" in obj:
                    # Save previous section
                    if current_table and current_rows:
                        table_batches.append((current_table, current_rows))
                    current_table = obj["__wal_table__"]
                    current_rows = []
                else:
                    current_rows.append(obj)

            # Save last section
            if current_table and current_rows:
                table_batches.append((current_table, current_rows))

            # Insert each table section
            for target_table, rows in table_batches:
                # Map batch table name to loader table name
                parsed_table = self._parse_batch_table_name(target_table)
                success = self._insert_with_dedup(parsed_table, rows, fname)
                if not success:
                    self._write_to_dlq(parsed_table, rows, "insert_failed_after_retries")
                    return False
        else:
            # Single-table file (legacy format)
            target_table = self._parse_table_from_filename(fname)
            if target_table == "unknown":
                logger.warning("Unknown table for file", file=fname)
                return False

            success = self._insert_with_dedup(target_table, all_lines, fname)
            if not success:
                self._write_to_dlq(target_table, all_lines, "insert_failed_after_retries")
                return False

        # Move to archive
        try:
            shutil.move(fpath, os.path.join(self.archive_dir, fname))
            logger.info("Archived file", file=fname)
            self._mark_processed(fpath)

            # EC-2: Track last processed timestamp
            file_ts = self._extract_file_ts(fname)
            if file_ts > self._last_processed_ts:
                self._last_processed_ts = file_ts
            return True
        except FileNotFoundError:
            return False

    def _insert_with_dedup(self, target_table: str, rows: list, fname: str) -> bool:
        """Insert rows with optional dedup guard (EC-1). Returns True on success."""
        if not rows:
            return True

        if self._dedup_enabled and self.ch_client:
            import hashlib

            raw = "".join(str(r) for r in rows)
            content_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
            if self._is_duplicate(target_table, content_hash):
                logger.info("Skipping duplicate WAL batch", file=fname, table=target_table, hash=content_hash)
                return True
            success = self.insert_batch(target_table, rows)
            if success:
                self._record_dedup(target_table, content_hash, len(rows))
            return success
        else:
            return self.insert_batch(target_table, rows)

    @staticmethod
    def _parse_batch_table_name(table_name: str) -> str:
        """Map batch writer table names (e.g. 'hft.market_data') to loader table names."""
        # Strip 'hft.' prefix if present
        if table_name.startswith("hft."):
            table_name = table_name[4:]
        # Map to canonical loader names
        mapping = {
            "market_data": "market_data",
            "orders": "orders",
            "trades": "trades",
            "fills": "trades",
            "risk_log": "risk_log",
            "logs": "risk_log",
            "backtest_runs": "backtest_runs",
            "latency_spans": "latency_spans",
        }
        return mapping.get(table_name, table_name)

    def _is_duplicate(self, table: str, content_hash: str) -> bool:
        """Check if WAL content hash already inserted (EC-1)."""
        try:
            with self._ch_lock:
                result = self.ch_client.command(
                    f"SELECT count() FROM hft._wal_dedup WHERE table = '{table}' AND hash = '{content_hash}'"
                )
            return int(result) > 0
        except Exception:
            return False  # Fail open

    def _record_dedup(self, table: str, content_hash: str, row_count: int) -> None:
        """Record WAL content hash after successful insert (EC-1)."""
        try:
            with self._ch_lock:
                self.ch_client.insert(
                    "hft._wal_dedup",
                    [[table, content_hash, row_count, timebase.now_ns()]],
                    column_names=["table", "hash", "row_count", "ts"],
                )
        except Exception as e:
            logger.warning("Failed to record dedup hash", error=str(e))

    def process_files(self, force: bool = False):
        """Process pending WAL files and load to ClickHouse.

        CC-3: Supports parallel file processing via ThreadPoolExecutor.

        Args:
            force: If True, skip mtime check and process all files immediately.
                   Used by WAL scheduler for batch flush at market close.
        """
        files = self._get_new_files()
        if not files:
            return

        processed = 0

        if self._loader_concurrency > 1 and len(files) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=self._loader_concurrency) as pool:
                futures = {pool.submit(self._process_single_file, f, force): f for f in files}
                for future in as_completed(futures):
                    fpath = futures[future]
                    try:
                        if future.result():
                            processed += 1
                    except Exception as e:
                        logger.error("File processing failed", file=fpath, error=str(e))
        else:
            for fpath in files:
                try:
                    if self._process_single_file(fpath, force):
                        processed += 1
                except Exception as e:
                    logger.error("File processing failed", file=fpath, error=str(e))

        if processed and self._manifest_enabled:
            self._save_manifest()

    def _quarantine_corrupt_file(self, fpath: str, fname: str, reason: str) -> None:
        """Move corrupt WAL file to quarantine directory."""
        os.makedirs(self.corrupt_dir, exist_ok=True)
        try:
            dest_path = os.path.join(self.corrupt_dir, fname)
            shutil.move(fpath, dest_path)
            logger.error("Moved corrupt WAL to quarantine", file=fname, reason=reason, dest=dest_path)
        except Exception as e:
            logger.error("Failed to quarantine corrupt file", file=fname, error=str(e))

    def _cleanup_old_dlq_files(self) -> None:
        """Remove or archive DLQ files older than retention period (B3)."""
        now = timebase.now_s()
        if now - self._last_dlq_cleanup_ts < self._dlq_cleanup_interval_s:
            return
        self._last_dlq_cleanup_ts = now

        if not os.path.isdir(self.dlq_dir):
            return

        retention_seconds = self._dlq_retention_days * 86400
        cutoff_ts = now - retention_seconds
        archived = 0
        deleted = 0

        try:
            for fname in os.listdir(self.dlq_dir):
                fpath = os.path.join(self.dlq_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime >= cutoff_ts:
                        continue

                    if self._dlq_archive_path:
                        os.makedirs(self._dlq_archive_path, exist_ok=True)
                        dest = os.path.join(self._dlq_archive_path, fname)
                        shutil.move(fpath, dest)
                        archived += 1
                    else:
                        os.remove(fpath)
                        deleted += 1
                except Exception as e:
                    logger.warning("Failed to clean up DLQ file", file=fname, error=str(e))

            if archived or deleted:
                logger.info(
                    "DLQ cleanup completed",
                    archived=archived,
                    deleted=deleted,
                    retention_days=self._dlq_retention_days,
                )
        except Exception as e:
            logger.warning("DLQ cleanup failed", error=str(e))

    def _cleanup_old_corrupt_files(self) -> None:
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

    def _check_wal_accumulation(self) -> None:
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

    def _write_to_dlq(self, table: str, rows: List[Dict[str, Any]], error: str) -> None:
        """Write failed rows to Dead Letter Queue for later analysis."""
        os.makedirs(self.dlq_dir, exist_ok=True)
        ts = int(timebase.now_ns())
        dlq_file = os.path.join(self.dlq_dir, f"{table}_{ts}.jsonl")
        try:
            with open(dlq_file, "w") as f:
                # Write metadata header
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
                # Write rows
                for row in rows:
                    f.write(_dumps(row) + "\n")
            logger.warning("Wrote failed batch to DLQ", table=table, count=len(rows), file=dlq_file)
        except Exception as e:
            logger.error("Failed to write to DLQ", table=table, error=str(e))

    def _compute_insert_backoff(self, attempt: int) -> float:
        """Compute backoff delay for insert retry."""
        import random

        delay = min(self._insert_base_delay_s * (2**attempt), self._insert_max_backoff_s)
        jitter = delay * 0.25 * (random.random() * 2 - 1)
        return max(0.1, delay + jitter)

    def insert_batch(self, table: str, rows: List[Dict[str, Any]]) -> bool:
        """Insert batch with retry logic. Returns True on success, False if all retries failed."""
        if not rows:
            return True

        # ClickHouse scale factor for price_scaled columns
        PRICE_SCALE = 1_000_000

        def _to_scaled(val: float | int | None) -> int:
            if val is None:
                return 0
            return int(round(float(val) * PRICE_SCALE))

        # Let's try to do it right for market_data
        if table == "market_data":
            data = []
            cols = [
                "symbol",
                "exchange",
                "type",
                "exch_ts",
                "ingest_ts",
                "price_scaled",
                "volume",
                "bids_price",
                "bids_vol",
                "asks_price",
                "asks_vol",
                "seq_no",
            ]

            for r in rows:
                meta = r.get("meta") or {}
                ts = int(
                    r.get("exch_ts")
                    or r.get("ts")
                    or r.get("timestamp")
                    or r.get("event_ts")
                    or meta.get("source_ts")
                    or 0
                )
                ingest_ts = int(
                    r.get("recv_ts")
                    or r.get("ingest_ts")
                    or r.get("ts")
                    or r.get("timestamp")
                    or meta.get("local_ts")
                    or timebase.now_ns()
                )

                # Check if data is already scaled (new format) or float (legacy)
                price_scaled = r.get("price_scaled")
                bids_price = r.get("bids_price") or r.get("bid_price")
                asks_price = r.get("asks_price") or r.get("ask_price")
                bids_vol = r.get("bids_vol") or r.get("bid_vol")
                asks_vol = r.get("asks_vol") or r.get("ask_vol")

                # Normalize bid/ask arrays when provided as [[price, vol], ...]
                raw_bids = r.get("bids")
                raw_asks = r.get("asks")
                if raw_bids and isinstance(raw_bids, (list, tuple)) and isinstance(raw_bids[0], (list, tuple)):
                    bids_price = [_to_scaled(p[0]) for p in raw_bids]
                    bids_vol = [int(p[1]) for p in raw_bids]
                if raw_asks and isinstance(raw_asks, (list, tuple)) and isinstance(raw_asks[0], (list, tuple)):
                    asks_price = [_to_scaled(p[0]) for p in raw_asks]
                    asks_vol = [int(p[1]) for p in raw_asks]

                # Convert float arrays to scaled int arrays (legacy support)
                if bids_price and isinstance(bids_price[0], float):
                    bids_price = [_to_scaled(p) for p in bids_price]
                if asks_price and isinstance(asks_price[0], float):
                    asks_price = [_to_scaled(p) for p in asks_price]

                best_bid = r.get("best_bid") or (bids_price[0] if bids_price else None)
                best_ask = r.get("best_ask") or (asks_price[0] if asks_price else None)

                # Handle price: prefer price_scaled, fallback to scaling float price
                if price_scaled is None:
                    price_float = r.get("price") or r.get("mid_price")
                    if price_float is None and best_bid is not None and best_ask is not None:
                        # best_bid/ask might be scaled or float
                        if isinstance(best_bid, int) and best_bid > 10000:
                            price_scaled = (best_bid + best_ask) // 2
                        else:
                            price_scaled = _to_scaled((float(best_bid) + float(best_ask)) / 2)
                    elif price_float is not None:
                        price_scaled = _to_scaled(price_float)
                    else:
                        price_scaled = 0

                # If we only have top-of-book, still store it as depth-1 arrays
                if not bids_price and best_bid is not None:
                    bids_price = [_to_scaled(best_bid) if isinstance(best_bid, float) else int(best_bid)]
                    bids_vol = [int(r.get("bid_depth") or 0)]
                if not asks_price and best_ask is not None:
                    asks_price = [_to_scaled(best_ask) if isinstance(best_ask, float) else int(best_ask)]
                    asks_vol = [int(r.get("ask_depth") or 0)]

                # Ensure ingest_ts is not earlier than exchange ts to avoid negative lag
                if ts:
                    if _TS_MAX_FUTURE_NS:
                        now_ns = timebase.now_ns()
                        if ts - now_ns > _TS_MAX_FUTURE_NS:
                            logger.warning(
                                "Exchange timestamp in future",
                                symbol=r.get("symbol"),
                                delta_ns=ts - now_ns,
                                max_future_ns=_TS_MAX_FUTURE_NS,
                            )
                            ts = now_ns
                    if ingest_ts < ts:
                        ingest_ts = ts

                # Minimal validation for missing book data
                if not bids_price or not asks_price:
                    logger.warning(
                        "Missing orderbook side in WAL row",
                        symbol=r.get("symbol"),
                        has_bids=bool(bids_price),
                        has_asks=bool(asks_price),
                    )

                row_data = [
                    r.get("symbol", ""),
                    r.get("exchange", r.get("exch", "TSE")),
                    r.get("type", meta.get("topic", "")),
                    ts,
                    ingest_ts,
                    int(price_scaled),
                    int(r.get("volume", r.get("total_volume", 0)) or 0),
                    bids_price or [],
                    bids_vol or [],
                    asks_price or [],
                    asks_vol or [],
                    int(r.get("seq_no", r.get("seq") or 0)),
                ]
                data.append(row_data)

            return self._insert_with_retry("hft.market_data", cols, data, table, len(rows))

        # Handle orders table
        elif table == "orders":
            data = []
            cols = [
                "order_id",
                "strategy_id",
                "symbol",
                "exchange",
                "side",
                "price_scaled",
                "qty",
                "order_type",
                "status",
                "exch_ts",
                "ingest_ts",
            ]
            for r in rows:
                price = r.get("price_scaled")
                if price is None:
                    price_float = r.get("price")
                    price = _to_scaled(price_float) if price_float is not None else 0

                exch_ts = int(r.get("exch_ts") or r.get("ts") or r.get("timestamp") or 0)
                ingest_ts = int(r.get("ingest_ts") or r.get("recv_ts") or timebase.now_ns())

                row_data = [
                    str(r.get("order_id", "")),
                    str(r.get("strategy_id", "")),
                    str(r.get("symbol", "")),
                    str(r.get("exchange", r.get("exch", ""))),
                    str(r.get("side", r.get("action", ""))),
                    int(price),
                    int(r.get("qty", r.get("quantity", 0)) or 0),
                    str(r.get("order_type", r.get("type", ""))),
                    str(r.get("status", "")),
                    exch_ts,
                    ingest_ts,
                ]
                data.append(row_data)

            return self._insert_with_retry("hft.orders", cols, data, table, len(rows))

        # Handle trades/fills table
        elif table == "trades":
            data = []
            cols = [
                "trade_id",
                "order_id",
                "symbol",
                "exchange",
                "side",
                "price_scaled",
                "qty",
                "exch_ts",
                "ingest_ts",
            ]
            for r in rows:
                price = r.get("price_scaled")
                if price is None:
                    price_float = r.get("price")
                    price = _to_scaled(price_float) if price_float is not None else 0

                exch_ts = int(r.get("exch_ts") or r.get("ts") or r.get("timestamp") or 0)
                ingest_ts = int(r.get("ingest_ts") or r.get("recv_ts") or timebase.now_ns())

                row_data = [
                    str(r.get("trade_id", r.get("fill_id", ""))),
                    str(r.get("order_id", "")),
                    str(r.get("symbol", "")),
                    str(r.get("exchange", r.get("exch", ""))),
                    str(r.get("side", r.get("action", ""))),
                    int(price),
                    int(r.get("qty", r.get("quantity", 0)) or 0),
                    exch_ts,
                    ingest_ts,
                ]
                data.append(row_data)

            return self._insert_with_retry("hft.trades", cols, data, table, len(rows))

        # Handle risk_log table
        elif table == "risk_log":
            data = []
            cols = [
                "ts",
                "strategy_id",
                "metric",
                "value",
                "context",
            ]
            for r in rows:
                ts = int(r.get("ts") or r.get("timestamp") or r.get("ingest_ts") or timebase.now_ns())
                context = r.get("context", {})
                if isinstance(context, dict):
                    context = _dumps(context)

                row_data = [
                    ts,
                    str(r.get("strategy_id", "")),
                    str(r.get("metric", "")),
                    float(r.get("value", 0)),
                    str(context),
                ]
                data.append(row_data)

            return self._insert_with_retry("hft.risk_log", cols, data, table, len(rows))

        # Handle backtest_runs table
        elif table == "backtest_runs":
            data = []
            cols = [
                "run_id",
                "strategy_id",
                "start_ts",
                "end_ts",
                "params",
                "metrics",
            ]
            for r in rows:
                params = r.get("params", {})
                if isinstance(params, dict):
                    params = _dumps(params)
                metrics = r.get("metrics", {})
                if isinstance(metrics, dict):
                    metrics = _dumps(metrics)

                row_data = [
                    str(r.get("run_id", "")),
                    str(r.get("strategy_id", "")),
                    int(r.get("start_ts", 0)),
                    int(r.get("end_ts", 0)),
                    str(params),
                    str(metrics),
                ]
                data.append(row_data)

            return self._insert_with_retry("hft.backtest_runs", cols, data, table, len(rows))

        else:
            # Unknown table - log warning and return False to trigger DLQ
            logger.warning(
                "No insert logic for table",
                table=table,
                count=len(rows),
            )
            return False

    def _insert_with_retry(
        self, full_table_name: str, cols: list, data: list, table_alias: str, row_count: int
    ) -> bool:
        """Insert data with retry logic. Returns True on success, False if all retries failed."""
        if not data:
            return True

        if self.ch_client and data:
            last_error = None
            for attempt in range(self._insert_max_retries):
                try:
                    with self._ch_lock:
                        self.ch_client.insert(full_table_name, data, column_names=cols)
                    logger.info("Inserted batch", table=table_alias, count=row_count)
                    if self.metrics:
                        if attempt > 0:
                            self.metrics.recorder_insert_retry_total.labels(table=table_alias, result="success").inc()
                        # CE3-06: throughput counter
                        self.metrics.wal_replay_throughput_rows_total.inc(row_count)
                    return True
                except Exception as e:
                    last_error = e
                    if attempt < self._insert_max_retries - 1:
                        if self.metrics:
                            self.metrics.recorder_insert_retry_total.labels(table=table_alias, result="retry").inc()
                        delay = self._compute_insert_backoff(attempt)
                        logger.warning(
                            "Insert failed, retrying with backoff",
                            table=table_alias,
                            attempt=attempt + 1,
                            delay_s=round(delay, 2),
                            error=str(e),
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "Insert failed after max retries",
                            table=table_alias,
                            max_retries=self._insert_max_retries,
                            error=str(last_error),
                        )
                        if self.metrics:
                            self.metrics.recorder_insert_retry_total.labels(table=table_alias, result="failed").inc()
                        return False
        elif data:
            # No client but we have data - also a "failure" for DLQ purposes
            logger.warning("No ClickHouse client available for insert", table=table_alias, count=len(data))
            return False

        return True


if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging

    configure_logging()
    loader = WALLoaderService()
    if loader._async_enabled:
        asyncio.run(loader.run_async())
    else:
        loader.run()
