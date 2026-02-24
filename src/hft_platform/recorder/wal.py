import asyncio
import fcntl
import os
import tempfile
import threading
import time
from glob import glob
from typing import Any

from structlog import get_logger

try:
    import orjson

    def _dumps(obj: object) -> str:
        return orjson.dumps(obj).decode()

    def _dumps_bytes(obj: object) -> bytes:
        return orjson.dumps(obj)

    _loads = orjson.loads
except ImportError:
    import json

    _dumps = json.dumps

    def _dumps_bytes(obj: object) -> bytes:
        return json.dumps(obj).encode()

    _loads = json.loads

from hft_platform.core import timebase
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("recorder.wal")


class WALWriter:
    def __init__(self, wal_dir: str):
        self.wal_dir = wal_dir
        os.makedirs(wal_dir, exist_ok=True)
        self._lock_fd = None
        # Disk space circuit breaker (EC-4)
        self._disk_min_mb = float(os.getenv("HFT_WAL_DISK_MIN_MB", "500"))
        self._disk_full = False
        self._disk_check_interval_s = 60.0
        self._last_disk_check_ts = 0.0
        self._disk_full_count = 0
        self._disk_pressure_policy = os.getenv("HFT_WAL_DISK_PRESSURE_POLICY", "drop").strip().lower()
        self._fsync_file_enabled = os.getenv("HFT_WAL_FILE_FSYNC", "1").lower() not in {"0", "false", "no", "off"}
        self._dir_fsync_min_ms = float(os.getenv("HFT_WAL_DIR_FSYNC_MIN_MS", "0") or "0")
        self._last_dir_fsync_ts = 0.0
        self._fsync_state_lock = threading.Lock()
        try:
            self._metrics = MetricsRegistry.get()
        except Exception:
            self._metrics = None

    def _set_disk_pressure_metrics(self, avail_mb: float | None, active: bool, writer: str) -> None:
        if not self._metrics:
            return
        try:
            if avail_mb is not None:
                self._metrics.wal_disk_available_mb.set(float(avail_mb))
            self._metrics.wal_disk_circuit_breaker_active.labels(writer=writer).set(1 if active else 0)
            self._metrics.disk_pressure_level.set(2 if active else 0)
        except Exception:
            return

    def _record_wal_write_latency(self, writer: str, mode: str, elapsed_ms: float) -> None:
        if not self._metrics:
            return
        try:
            self._metrics.recorder_wal_write_latency_ms.labels(writer=writer, mode=mode).observe(elapsed_ms)
        except Exception:
            return

    def _record_fsync_latency(self, writer: str, target: str, elapsed_ms: float) -> None:
        if not self._metrics:
            return
        try:
            self._metrics.recorder_wal_fsync_latency_ms.labels(writer=writer, target=target).observe(elapsed_ms)
        except Exception:
            return

    def _handle_disk_pressure_skip(self, table: str, rows: int, *, writer: str) -> bool:
        self._disk_full_count += rows
        if self._metrics:
            try:
                self._metrics.recorder_wal_skipped_rows_total.labels(
                    writer=writer, table=table, reason="disk_full"
                ).inc(rows)
            except Exception:
                pass
        logger.warning(
            "WAL write skipped - disk full circuit breaker active",
            table=table,
            rows_skipped=rows,
            total_skipped=self._disk_full_count,
            policy=self._disk_pressure_policy,
            writer=writer,
        )
        if self._disk_pressure_policy == "raise":
            raise RuntimeError("WAL disk pressure circuit breaker active")
        return False

    def _maybe_fsync_file(self, fd: int, *, writer: str) -> None:
        if not self._fsync_file_enabled:
            return
        t0 = time.monotonic()
        os.fsync(fd)
        self._record_fsync_latency(writer, "file", (time.monotonic() - t0) * 1000.0)

    def _maybe_fsync_dir(self, dir_path: str, *, writer: str) -> None:
        now = time.monotonic()
        with self._fsync_state_lock:
            if self._dir_fsync_min_ms > 0 and (now - self._last_dir_fsync_ts) * 1000.0 < self._dir_fsync_min_ms:
                return
            self._last_dir_fsync_ts = now
        dir_fd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            t0 = time.monotonic()
            os.fsync(dir_fd)
            self._record_fsync_latency(writer, "dir", (time.monotonic() - t0) * 1000.0)
        finally:
            os.close(dir_fd)

    def _check_disk_space(self) -> bool:
        """Check available disk space; return True if sufficient."""
        now = time.monotonic()
        if now - self._last_disk_check_ts < self._disk_check_interval_s:
            return not self._disk_full
        self._last_disk_check_ts = now
        try:
            stat = os.statvfs(self.wal_dir)
            avail_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            self._set_disk_pressure_metrics(avail_mb, avail_mb < self._disk_min_mb, "wal")
            if avail_mb < self._disk_min_mb:
                if not self._disk_full:
                    logger.critical(
                        "WAL disk space below threshold, activating circuit breaker",
                        avail_mb=round(avail_mb, 1),
                        threshold_mb=self._disk_min_mb,
                    )
                self._disk_full = True
                return False
            if self._disk_full:
                logger.info(
                    "WAL disk space recovered, deactivating circuit breaker",
                    avail_mb=round(avail_mb, 1),
                )
            self._disk_full = False
            return True
        except OSError:
            self._set_disk_pressure_metrics(None, False, "wal")
            return True  # Fail open if statvfs unavailable

    async def write(self, table: str, data: list) -> bool:
        """Async append to local disk via thread pool with atomic write.

        Returns True if written, False if skipped (disk full).
        """
        if not self._check_disk_space():
            return self._handle_disk_pressure_skip(table, len(data), writer="wal")

        ts = int(timebase.now_ns())
        filename = f"{self.wal_dir}/{table}_{ts}.jsonl"

        loop = asyncio.get_running_loop()
        try:
            t0 = time.monotonic()
            await loop.run_in_executor(None, self._write_sync_atomic, filename, data)
            self._record_wal_write_latency("wal", "atomic", (time.monotonic() - t0) * 1000.0)
            logger.info("Wrote to WAL", table=table, count=len(data), file=filename)
            return True
        except Exception as e:
            logger.critical("WAL Write Failed!", error=str(e))
            return False

    def _write_sync_atomic(self, filename: str, data: list):
        """
        Atomic write: write to temp file, then rename.
        This prevents partial reads by the loader.
        """
        # Write to temp file in same directory (for atomic rename)
        dir_path = os.path.dirname(filename)
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_path)
        try:
            with os.fdopen(fd, "w") as f:
                # Acquire exclusive lock during write
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    for row in data:
                        f.write(_dumps(row) + "\n")
                    f.flush()
                    self._maybe_fsync_file(f.fileno(), writer="wal")
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            # Atomic rename (on POSIX systems)
            os.rename(tmp_path, filename)
            # fsync directory to ensure rename is durable on disk (coalesced when configured)
            self._maybe_fsync_dir(dir_path, writer="wal")
        except Exception:
            # Clean up temp file on failure
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _write_sync(self, filename: str, data: list):
        """Legacy blocking write (kept for compatibility)."""
        with open(filename, "w") as f:
            for row in data:
                f.write(_dumps(row) + "\n")


class WALBatchWriter:
    """Coalesces multiple tables into one multi-table WAL file (CC-4, EC-3).

    Instead of creating a separate WAL file per table per flush,
    buffers rows and writes a single multi-table file per time window.
    Each table section is prefixed with a JSON header line:
        {"__wal_table__":"table_name","__row_count__":N}

    EC-3: Splits into new file when size exceeds HFT_WAL_FILE_MAX_MB.
    """

    def __init__(self, wal_dir: str):
        self._wal_dir = wal_dir
        os.makedirs(wal_dir, exist_ok=True)

        # Coalescing configuration
        self._batch_interval_ms = int(os.getenv("HFT_WAL_BATCH_INTERVAL_MS", "1000"))
        self._batch_max_rows = int(os.getenv("HFT_WAL_BATCH_MAX_ROWS", "5000"))
        self._file_max_bytes = int(float(os.getenv("HFT_WAL_FILE_MAX_MB", "50")) * 1024 * 1024)

        # Internal state
        self._buffer: dict[str, list[dict[str, Any]]] = {}
        self._columnar_buffer: dict[str, list[tuple[list[str], list[list[Any]], int]]] = {}
        self._buffer_rows = 0
        self._buffer_bytes = 0  # Approximate
        self._lock = threading.Lock()
        self._last_flush_ts = time.monotonic()

        # Disk space check (reuse WALWriter's approach)
        self._disk_min_mb = float(os.getenv("HFT_WAL_DISK_MIN_MB", "500"))
        self._disk_full = False
        self._disk_check_interval_s = 60.0
        self._last_disk_check_ts = 0.0
        self._disk_full_count = 0
        self._disk_pressure_policy = os.getenv("HFT_WAL_DISK_PRESSURE_POLICY", "drop").strip().lower()
        self._fsync_file_enabled = os.getenv("HFT_WAL_FILE_FSYNC", "1").lower() not in {"0", "false", "no", "off"}
        self._dir_fsync_min_ms = float(os.getenv("HFT_WAL_DIR_FSYNC_MIN_MS", "0") or "0")
        self._last_dir_fsync_ts = 0.0
        self._fsync_state_lock = threading.Lock()
        try:
            self._metrics = MetricsRegistry.get()
        except Exception:
            self._metrics = None

        # Background flush timer
        self._timer_running = True
        self._timer_thread = threading.Thread(
            target=self._flush_timer_loop,
            name="wal-batch-timer",
            daemon=True,
        )
        self._timer_thread.start()

    def _set_disk_pressure_metrics(self, avail_mb: float | None, active: bool) -> None:
        if not self._metrics:
            return
        try:
            if avail_mb is not None:
                self._metrics.wal_disk_available_mb.set(float(avail_mb))
            self._metrics.wal_disk_circuit_breaker_active.labels(writer="wal_batch").set(1 if active else 0)
            self._metrics.disk_pressure_level.set(2 if active else 0)
        except Exception:
            return

    def _record_wal_write_latency(self, mode: str, elapsed_ms: float) -> None:
        if not self._metrics:
            return
        try:
            self._metrics.recorder_wal_write_latency_ms.labels(writer="wal_batch", mode=mode).observe(elapsed_ms)
        except Exception:
            return

    def _record_fsync_latency(self, target: str, elapsed_ms: float) -> None:
        if not self._metrics:
            return
        try:
            self._metrics.recorder_wal_fsync_latency_ms.labels(writer="wal_batch", target=target).observe(elapsed_ms)
        except Exception:
            return

    def _handle_disk_pressure_skip(self, table: str, rows: int) -> bool:
        self._disk_full_count += rows
        if self._metrics:
            try:
                self._metrics.recorder_wal_skipped_rows_total.labels(
                    writer="wal_batch",
                    table=table,
                    reason="disk_full",
                ).inc(rows)
            except Exception:
                pass
        logger.warning(
            "WAL batch add skipped - disk full circuit breaker active",
            table=table,
            rows_skipped=rows,
            total_skipped=self._disk_full_count,
            policy=self._disk_pressure_policy,
        )
        if self._disk_pressure_policy == "raise":
            raise RuntimeError("WAL batch disk pressure circuit breaker active")
        return False

    def _maybe_fsync_file(self, fd: int) -> None:
        if not self._fsync_file_enabled:
            return
        t0 = time.monotonic()
        os.fsync(fd)
        self._record_fsync_latency("file", (time.monotonic() - t0) * 1000.0)

    def _maybe_fsync_dir(self, dir_path: str) -> None:
        now = time.monotonic()
        with self._fsync_state_lock:
            if self._dir_fsync_min_ms > 0 and (now - self._last_dir_fsync_ts) * 1000.0 < self._dir_fsync_min_ms:
                return
            self._last_dir_fsync_ts = now
        dir_fd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            t0 = time.monotonic()
            os.fsync(dir_fd)
            self._record_fsync_latency("dir", (time.monotonic() - t0) * 1000.0)
        finally:
            os.close(dir_fd)

    def _check_disk_space(self) -> bool:
        now = time.monotonic()
        if now - self._last_disk_check_ts < self._disk_check_interval_s:
            return not self._disk_full
        self._last_disk_check_ts = now
        try:
            stat = os.statvfs(self._wal_dir)
            avail_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            self._set_disk_pressure_metrics(avail_mb, avail_mb < self._disk_min_mb)
            was_full = self._disk_full
            self._disk_full = avail_mb < self._disk_min_mb
            if self._disk_full and not was_full:
                logger.critical(
                    "WAL batch disk space below threshold",
                    avail_mb=round(avail_mb, 1),
                )
            elif was_full and not self._disk_full:
                logger.info("WAL batch disk space recovered", avail_mb=round(avail_mb, 1))
            return not self._disk_full
        except OSError:
            self._set_disk_pressure_metrics(None, False)
            return True

    async def add(self, table: str, rows: list[dict[str, Any]]) -> bool:
        """Add rows to the coalescing buffer. Returns False if disk full."""
        if not self._check_disk_space():
            return self._handle_disk_pressure_skip(table, len(rows))

        should_flush = False
        with self._lock:
            if table not in self._buffer:
                self._buffer[table] = []
            self._buffer[table].extend(rows)
            self._buffer_rows += len(rows)
            # Approximate size tracking for EC-3 without serializing every row in hot path.
            # Real split sizing is enforced during _write_batch_sync.
            self._buffer_bytes += max(64, 96 * len(rows))

            if self._buffer_rows >= self._batch_max_rows:
                should_flush = True

        if should_flush:
            return await self.flush()

        return True

    async def add_columnar(
        self,
        table: str,
        column_names: list[str],
        column_data: list[list[Any]],
        row_count: int,
    ) -> bool:
        """Add columnar rows to buffer without reconstructing row dicts in hot path."""
        if row_count <= 0 or not column_names or not column_data:
            return True
        if not self._check_disk_space():
            return self._handle_disk_pressure_skip(table, row_count)

        should_flush = False
        with self._lock:
            self._columnar_buffer.setdefault(table, []).append((list(column_names), column_data, int(row_count)))
            self._buffer_rows += int(row_count)
            self._buffer_bytes += max(128, 64 * int(row_count))
            if self._buffer_rows >= self._batch_max_rows:
                should_flush = True

        if should_flush:
            return await self.flush()
        return True

    async def flush(self) -> bool:
        """Flush coalesced buffer to disk."""
        with self._lock:
            if (not self._buffer and not self._columnar_buffer) or self._buffer_rows == 0:
                return True
            # Grab buffer, reset
            flush_data = self._buffer
            flush_columnar = self._columnar_buffer
            self._buffer = {}
            self._columnar_buffer = {}
            flush_rows = self._buffer_rows
            flush_bytes = self._buffer_bytes
            self._buffer_rows = 0
            self._buffer_bytes = 0
            self._last_flush_ts = time.monotonic()

        loop = asyncio.get_running_loop()
        try:
            t0 = time.monotonic()
            await loop.run_in_executor(
                None,
                self._write_batch_sync,
                flush_data,
                flush_bytes,
                flush_columnar,
            )
            self._record_wal_write_latency("batch_flush", (time.monotonic() - t0) * 1000.0)
            logger.info(
                "WAL batch flush",
                tables=len(flush_data) + len(flush_columnar),
                rows=flush_rows,
            )
            if self._metrics:
                try:
                    self._metrics.wal_batch_flush_total.labels(result="ok").inc()
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.critical("WAL batch write failed", error=str(e))
            if self._metrics:
                try:
                    self._metrics.wal_batch_flush_total.labels(result="error").inc()
                except Exception:
                    pass
            return False

    def _write_batch_sync(
        self,
        data: dict[str, list[dict[str, Any]]],
        approx_bytes: int,
        columnar_data: dict[str, list[tuple[list[str], list[list[Any]], int]]] | None = None,
    ) -> None:
        """Write multi-table WAL file(s) atomically. EC-3: splits on size limit."""
        dir_path = self._wal_dir
        current_bytes = 0
        current_lines: list[bytes] = []
        file_count = 0
        columnar_data = columnar_data or {}

        def _flush_file() -> None:
            nonlocal current_bytes, current_lines, file_count
            if not current_lines:
                return
            ts = int(timebase.now_ns()) + file_count
            filename = f"{dir_path}/batch_{ts}.jsonl"
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_path)
            try:
                with os.fdopen(fd, "wb") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        for line in current_lines:
                            f.write(line)
                        f.flush()
                        self._maybe_fsync_file(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                os.rename(tmp_path, filename)
                self._maybe_fsync_dir(dir_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            file_count += 1
            current_lines = []
            current_bytes = 0

        for table, rows in data.items():
            if not rows:
                continue

            # Write table header
            header = _dumps_bytes({"__wal_table__": table, "__row_count__": len(rows)}) + b"\n"
            header_bytes = len(header)

            # EC-3: Check if adding this table would exceed file limit
            if current_bytes > 0 and current_bytes + header_bytes > self._file_max_bytes:
                _flush_file()

            current_lines.append(header)
            current_bytes += header_bytes

            for idx, row in enumerate(rows):
                line = _dumps_bytes(row) + b"\n"
                line_bytes = len(line)

                # EC-3: Split mid-table if needed
                if current_bytes + line_bytes > self._file_max_bytes and current_bytes > 0:
                    _flush_file()
                    # Re-add header for continuation
                    # (loader will see multiple headers for same table — that's fine)
                    remaining = len(rows) - idx
                    cont_header = _dumps_bytes({"__wal_table__": table, "__row_count__": remaining}) + b"\n"
                    current_lines.append(cont_header)
                    current_bytes += len(cont_header)

                current_lines.append(line)
                current_bytes += line_bytes

        for table, segments in columnar_data.items():
            for column_names, column_values, row_count in segments:
                if row_count <= 0:
                    continue
                header = _dumps_bytes({"__wal_table__": table, "__row_count__": row_count}) + b"\n"
                header_bytes = len(header)
                if current_bytes > 0 and current_bytes + header_bytes > self._file_max_bytes:
                    _flush_file()
                current_lines.append(header)
                current_bytes += header_bytes
                col_names = tuple(column_names)
                for idx in range(row_count):
                    row = {name: column_values[col_i][idx] for col_i, name in enumerate(col_names)}
                    line = _dumps_bytes(row) + b"\n"
                    line_bytes = len(line)
                    if current_bytes + line_bytes > self._file_max_bytes and current_bytes > 0:
                        _flush_file()
                        remaining = row_count - idx
                        cont_header = _dumps_bytes({"__wal_table__": table, "__row_count__": remaining}) + b"\n"
                        current_lines.append(cont_header)
                        current_bytes += len(cont_header)
                    current_lines.append(line)
                    current_bytes += line_bytes

        # Flush remaining
        _flush_file()

    def _flush_timer_loop(self) -> None:
        """Background timer to flush on interval."""
        while self._timer_running:
            time.sleep(self._batch_interval_ms / 1000.0)
            if not self._timer_running:
                break
            with self._lock:
                elapsed_ms = (time.monotonic() - self._last_flush_ts) * 1000
                if self._buffer_rows == 0 or elapsed_ms < self._batch_interval_ms:
                    continue
                # Need flush
                flush_data = self._buffer
                flush_columnar = self._columnar_buffer
                self._buffer = {}
                self._columnar_buffer = {}
                flush_rows = self._buffer_rows
                self._buffer_rows = 0
                self._buffer_bytes = 0
                self._last_flush_ts = time.monotonic()

            if flush_data or flush_columnar:
                try:
                    t0 = time.monotonic()
                    self._write_batch_sync(flush_data, 0, flush_columnar)
                    self._record_wal_write_latency("batch_timer_flush", (time.monotonic() - t0) * 1000.0)
                    logger.info(
                        "WAL batch timer flush",
                        tables=len(flush_data) + len(flush_columnar),
                        rows=flush_rows,
                    )
                    if self._metrics:
                        try:
                            self._metrics.wal_batch_flush_total.labels(result="ok").inc()
                        except Exception:
                            pass
                except Exception as e:
                    logger.error("WAL batch timer flush failed", error=str(e))
                    if self._metrics:
                        try:
                            self._metrics.wal_batch_flush_total.labels(result="error").inc()
                        except Exception:
                            pass

    def stop(self) -> None:
        """Stop the background timer and flush remaining data."""
        self._timer_running = False
        # Final sync flush
        with self._lock:
            if (self._buffer or self._columnar_buffer) and self._buffer_rows > 0:
                flush_data = self._buffer
                flush_columnar = self._columnar_buffer
                self._buffer = {}
                self._columnar_buffer = {}
                self._buffer_rows = 0
                self._buffer_bytes = 0
            else:
                flush_data = {}
                flush_columnar = {}
        if flush_data or flush_columnar:
            try:
                t0 = time.monotonic()
                self._write_batch_sync(flush_data, 0, flush_columnar)
                self._record_wal_write_latency("batch_stop_flush", (time.monotonic() - t0) * 1000.0)
            except Exception as e:
                logger.error("WAL batch final flush failed", error=str(e))


class WALReplayer:
    def __init__(self, wal_dir: str, sender_func):
        self.wal_dir = wal_dir
        self.sender_func = sender_func  # Async function(table, data) -> bool

    async def replay(self):
        """Scans directory and attempts to replay files."""
        files = sorted(glob(f"{self.wal_dir}/*.jsonl"))
        if not files:
            return

        logger.info("Found WAL files", count=len(files))

        for fpath in files:
            # Parse table name from filename provided it matches table_timestamp.jsonl
            fname = os.path.basename(fpath)
            table = fname.rsplit("_", 1)[0]

            try:
                data = []
                with open(fpath, "r") as f:
                    for line in f:
                        if line.strip():
                            data.append(_loads(line))

                if data:
                    success = await self.sender_func(table, data)
                    if success:
                        os.remove(fpath)
                        logger.info("Replayed and deleted WAL", file=fname)
                    else:
                        logger.warning("Replay failed, keeping WAL", file=fname)
                        break  # Stop on first failure to preserve order?
                else:
                    # Empty file
                    os.remove(fpath)
            except Exception as e:
                logger.error("Corrupt WAL file", file=fname, error=str(e))
                # Move to corrupt dir?
