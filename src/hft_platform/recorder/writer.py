import asyncio
import os
import random
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.recorder.schema import apply_schema, ensure_price_scaled_views
from hft_platform.recorder.wal import WALWriter

# import clickhouse_connect # Mocked for now if not available in env
try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect = None  # type: ignore[assignment]

logger = get_logger("recorder.writer")


class DataWriter:
    # Default to native protocol (port 9000) for better performance
    # HTTP protocol (8123) is slower but more compatible
    DEFAULT_NATIVE_PORT = 9000
    DEFAULT_HTTP_PORT = 8123

    # Exponential backoff configuration
    DEFAULT_MAX_RETRIES = 5
    DEFAULT_BASE_DELAY_S = 1.0
    DEFAULT_MAX_BACKOFF_S = 30.0
    DEFAULT_JITTER_FACTOR = 0.5

    def __init__(self, ch_host="localhost", ch_port=9000, wal_dir=".wal"):
        self.ch_client = None
        self.wal = WALWriter(wal_dir)
        # Per-table lock striping: avoid serializing inserts across different tables
        self._ch_locks: dict[str, threading.Lock] = {}
        self._ch_locks_guard = threading.Lock()
        self._ch_heartbeat_lock = threading.Lock()
        # Determine protocol based on port (9000=native, 8123=HTTP)
        use_native = ch_port == self.DEFAULT_NATIVE_PORT
        ch_username = (
            os.getenv("HFT_CLICKHOUSE_USER")
            or os.getenv("HFT_CLICKHOUSE_USERNAME")
            or os.getenv("CLICKHOUSE_USER")
            or os.getenv("CLICKHOUSE_USERNAME")
            or "default"
        )
        ch_password = os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD") or ""
        self.ch_params = {
            "host": ch_host,
            "port": ch_port,
            "username": ch_username,
            "password": ch_password,
            "compress": True,  # Enable compression for native protocol
        }
        # Native protocol uses 'interface' parameter
        if use_native:
            self.ch_params["interface"] = "native"
        self.connected = False
        self._schema_initialized = False
        # ClickHouse is opt-in; enable by setting HFT_CLICKHOUSE_ENABLED=1
        self.ch_enabled = str(os.getenv("HFT_CLICKHOUSE_ENABLED", "")).lower() in ("1", "true", "yes", "on")
        if os.getenv("HFT_DISABLE_CLICKHOUSE"):
            self.ch_enabled = False
        # Allow host/port override via env
        self.ch_params["host"] = os.getenv("HFT_CLICKHOUSE_HOST", self.ch_params["host"])
        env_port = os.getenv("HFT_CLICKHOUSE_PORT")
        if env_port:
            self.ch_params["port"] = int(env_port)
            # Re-check if native based on env port
            if int(env_port) == self.DEFAULT_NATIVE_PORT:
                self.ch_params["interface"] = "native"
            elif "interface" in self.ch_params:
                del self.ch_params["interface"]

        # EC-2: CH insert timeout
        self._insert_timeout_s = float(os.getenv("HFT_CH_INSERT_TIMEOUT_S", "30"))
        self._insert_warn_ms = int(os.getenv("HFT_CH_INSERT_WARN_MS", "5000"))
        # Apply send_receive_timeout to CH params
        self.ch_params["send_receive_timeout"] = int(self._insert_timeout_s)

        # CC-3: Bounded thread pool for CH inserts
        self._pool_size = int(os.getenv("HFT_CH_INSERT_POOL_SIZE", "8"))
        self._max_concurrent_inserts = int(os.getenv("HFT_CH_MAX_CONCURRENT_INSERTS", "6"))
        self._executor = ThreadPoolExecutor(
            max_workers=self._pool_size,
            thread_name_prefix="ch-insert",
        )
        self._insert_semaphore = asyncio.Semaphore(self._max_concurrent_inserts)

        # CC-4: WAL batch writer (lazy init)
        self._wal_batch_writer: Any = None
        self._wal_batch_enabled = os.getenv("HFT_WAL_BATCH_ENABLED", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # EC-5: Health tracker reference
        self._health_tracker: Any = None

        # Exponential backoff settings (configurable via env)
        self._max_retries = int(os.getenv("HFT_CH_MAX_RETRIES", str(self.DEFAULT_MAX_RETRIES)))
        self._base_delay_s = float(os.getenv("HFT_CH_BASE_DELAY_S", str(self.DEFAULT_BASE_DELAY_S)))
        self._max_backoff_s = float(os.getenv("HFT_CH_MAX_BACKOFF_S", str(self.DEFAULT_MAX_BACKOFF_S)))
        self._jitter_factor = float(os.getenv("HFT_CH_JITTER_FACTOR", str(self.DEFAULT_JITTER_FACTOR)))
        self._connect_attempts = 0
        try:
            self._ts_max_future_ns = int(float(os.getenv("HFT_TS_MAX_FUTURE_S", "5")) * 1e9)
        except ValueError as exc:
            logger.warning("Failed to parse HFT_TS_MAX_FUTURE_S, disabling future filter", error=str(exc))
            self._ts_max_future_ns = 0

        # Heartbeat configuration (B4)
        self._heartbeat_interval_s = float(os.getenv("HFT_CH_HEARTBEAT_INTERVAL_S", "30"))
        self._heartbeat_timeout_s = float(os.getenv("HFT_CH_HEARTBEAT_TIMEOUT_S", "5"))
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_running = False
        self._last_heartbeat_ts = 0.0
        self._last_heartbeat_ok = True
        self._reconnect_lock = threading.Lock()
        self._reconnect_running = False
        self._last_reconnect_ts = 0.0
        self._reconnect_min_interval_s = float(os.getenv("HFT_CH_RECONNECT_MIN_S", "5"))

        try:
            from hft_platform.observability.metrics import MetricsRegistry

            self.metrics = MetricsRegistry.get()
        except Exception:
            self.metrics = None

    def set_health_tracker(self, tracker: Any) -> None:
        self._health_tracker = tracker

    def _get_wal_batch_writer(self) -> Any:
        """Lazy-init WAL batch writer (CC-4)."""
        if self._wal_batch_writer is None and self._wal_batch_enabled:
            try:
                from hft_platform.recorder.wal import WALBatchWriter

                self._wal_batch_writer = WALBatchWriter(self.wal.wal_dir)
            except Exception:
                self._wal_batch_writer = None
        return self._wal_batch_writer

    def _get_table_lock(self, table: str) -> threading.Lock:
        """Get or create a per-table lock for ClickHouse inserts."""
        lock = self._ch_locks.get(table)
        if lock is not None:
            return lock
        with self._ch_locks_guard:
            if table not in self._ch_locks:
                self._ch_locks[table] = threading.Lock()
            return self._ch_locks[table]

    def _compute_backoff_delay(self, attempt: int) -> float:
        """Compute exponential backoff delay with jitter to avoid thundering herd."""
        # Exponential: base_delay * 2^attempt, capped at max_backoff
        delay = min(self._base_delay_s * (2**attempt), self._max_backoff_s)
        # Add jitter: delay * (1 +/- jitter_factor * random)
        jitter = delay * self._jitter_factor * (random.random() * 2 - 1)
        return max(0.1, delay + jitter)  # Minimum 100ms

    def connect(self):
        """Synchronous connect - use connect_async() in async contexts."""
        if not self.ch_enabled or not clickhouse_connect:
            logger.info("Running in WAL-only mode (ClickHouse disabled or driver missing)")
            return

        for attempt in range(self._max_retries):
            self._connect_attempts = attempt
            try:
                self.ch_client = clickhouse_connect.get_client(**self.ch_params)
                self.connected = True
                self._connect_attempts = 0  # Reset on success
                logger.info("Connected to ClickHouse", attempt=attempt + 1)
                if self.metrics:
                    self.metrics.clickhouse_connection_health.set(1)
                self._init_schema()
                self._start_heartbeat_thread()
                break

            except Exception as e:
                if attempt < self._max_retries - 1:
                    delay = self._compute_backoff_delay(attempt)
                    logger.warning(
                        "ClickHouse connection failed, retrying with backoff...",
                        error=str(e),
                        attempt=attempt + 1,
                        delay_s=round(delay, 2),
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "ClickHouse connection failed after max retries, falling back to WAL",
                        error=str(e),
                        max_retries=self._max_retries,
                    )
                    self.connected = False
                    if self.metrics:
                        self.metrics.clickhouse_connection_health.set(0)

    async def connect_async(self):
        """Async connect - does not block the event loop during retries."""
        if not self.ch_enabled or not clickhouse_connect:
            logger.info("Running in WAL-only mode (ClickHouse disabled or driver missing)")
            return

        for attempt in range(self._max_retries):
            self._connect_attempts = attempt
            try:
                self.ch_client = await asyncio.to_thread(clickhouse_connect.get_client, **self.ch_params)
                self.connected = True
                self._connect_attempts = 0  # Reset on success
                logger.info("Connected to ClickHouse", attempt=attempt + 1)
                if self.metrics:
                    self.metrics.clickhouse_connection_health.set(1)
                await asyncio.to_thread(self._init_schema)
                self._start_heartbeat_thread()
                break

            except Exception as e:
                if attempt < self._max_retries - 1:
                    delay = self._compute_backoff_delay(attempt)
                    logger.warning(
                        "ClickHouse connection failed, retrying with backoff...",
                        error=str(e),
                        attempt=attempt + 1,
                        delay_s=round(delay, 2),
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "ClickHouse connection failed after max retries, falling back to WAL",
                        error=str(e),
                        max_retries=self._max_retries,
                    )
                    self.connected = False
                    if self.metrics:
                        self.metrics.clickhouse_connection_health.set(0)

    def _init_schema(self):
        """Initialize ClickHouse schema from SQL file."""
        try:
            apply_schema(self.ch_client)
            self._schema_initialized = True
        except Exception as se:
            logger.critical(
                "Schema initialization failed - falling back to WAL-only mode",
                error=str(se),
                exc_info=True,
            )
            self._schema_initialized = False
            self.connected = False
            return

        try:
            ensure_price_scaled_views(self.ch_client)
        except Exception as se:
            logger.error("Schema view repair failed", error=str(se))
            # Views are optional, don't fail completely

    def _start_heartbeat_thread(self) -> None:
        """Start daemon thread for connection heartbeat."""
        if self._heartbeat_running:
            return
        if not self.ch_enabled or not self.connected:
            return
        self._heartbeat_running = True
        logger.info(
            "Starting ClickHouse heartbeat",
            interval_s=self._heartbeat_interval_s,
            timeout_s=self._heartbeat_timeout_s,
        )

        def _heartbeat_loop() -> None:
            try:
                while self._heartbeat_running and self.connected and self.ch_client:
                    time.sleep(self._heartbeat_interval_s)
                    if not self._heartbeat_running:
                        break
                    ok = self._do_heartbeat_check()
                    self._last_heartbeat_ts = timebase.now_s()
                    self._last_heartbeat_ok = ok
                    if not ok:
                        logger.error("ClickHouse heartbeat failed, marking connection as stale")
                        self.connected = False
                        self.ch_client = None
                        if self.metrics:
                            self.metrics.clickhouse_connection_health.set(0)
                        self._schedule_reconnect("heartbeat_failed")
                        break
            finally:
                self._heartbeat_running = False

        self._heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name="clickhouse-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _do_heartbeat_check(self) -> bool:
        """Execute SELECT 1 to verify connection health."""
        if not self.ch_client:
            return False
        try:
            with self._ch_heartbeat_lock:
                self.ch_client.command("SELECT 1")
            return True
        except Exception as e:
            logger.warning("ClickHouse heartbeat check failed", error=str(e))
            return False

    def _schedule_reconnect(self, reason: str) -> None:
        if not self.ch_enabled or not clickhouse_connect:
            return
        now = timebase.now_s()
        if now - self._last_reconnect_ts < self._reconnect_min_interval_s:
            return
        if self._reconnect_running:
            return
        if not self._reconnect_lock.acquire(blocking=False):
            return
        self._reconnect_running = True
        self._last_reconnect_ts = now
        logger.warning("Scheduling ClickHouse reconnect", reason=reason)

        def _do_reconnect() -> None:
            try:
                self.connect()
            finally:
                self._reconnect_running = False
                self._reconnect_lock.release()

        threading.Thread(
            target=_do_reconnect,
            name="clickhouse-reconnect",
            daemon=True,
        ).start()

    def get_status(self) -> dict:
        """Get current writer status for health checks."""
        return {
            "ch_enabled": self.ch_enabled,
            "connected": self.connected,
            "schema_initialized": self._schema_initialized,
            "wal_only_mode": not self.connected or not self._schema_initialized,
            "connect_attempts": self._connect_attempts,
            "ch_host": self.ch_params.get("host"),
            "ch_port": self.ch_params.get("port"),
            "last_heartbeat_ts": self._last_heartbeat_ts,
            "last_heartbeat_ok": self._last_heartbeat_ok,
        }

    async def write_columnar(
        self,
        table: str,
        column_names: list[str],
        column_data: list[list[Any]],
        row_count: int,
    ):
        """Write columnar data to CH (CC-1). Falls back to WAL on failure."""
        if not column_names or not column_data or row_count == 0:
            return

        column_data, row_count = self._sanitize_columnar(table, column_names, column_data, row_count)
        if row_count == 0:
            return

        success = False
        if self.connected and self.ch_client:
            try:
                async with self._insert_semaphore:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        self._executor,
                        self._ch_insert_columnar,
                        table,
                        column_names,
                        column_data,
                        row_count,
                    )
                success = True
            except (TimeoutError, socket.timeout) as e:
                logger.error(
                    "ClickHouse insert timeout",
                    table=table,
                    timeout_s=self._insert_timeout_s,
                    error=str(e),
                )
                if self._health_tracker:
                    self._health_tracker.record_event("ch_timeout", table=table)
                self._schedule_reconnect("insert_timeout")
                success = False
            except Exception as e:
                logger.error("ClickHouse write failed", table=table, error=str(e))
                if self._health_tracker:
                    self._health_tracker.record_event("ch_error", table=table)
                self._schedule_reconnect("write_failed")
                success = False
        else:
            if self.ch_enabled:
                self._schedule_reconnect("not_connected")

        if not success:
            logger.warning("Fallback to WAL (columnar)", table=table, count=row_count)
            if self.metrics:
                self.metrics.recorder_wal_writes_total.labels(table=table).inc()
            if self._health_tracker:
                self._health_tracker.record_event("wal_fallback", table=table, count=row_count)

            # Reconstruct row dicts for WAL
            row_dicts = []
            for i in range(row_count):
                row = {}
                for ci, col in enumerate(column_names):
                    row[col] = column_data[ci][i]
                row_dicts.append(row)

            # CC-4: Use batch writer if available
            batch_writer = self._get_wal_batch_writer()
            if batch_writer is not None:
                wal_ok = await batch_writer.add(table, row_dicts)
            else:
                wal_ok = await self.wal.write(table, row_dicts)

            if not wal_ok:
                logger.critical(
                    "Data loss: both ClickHouse and WAL failed",
                    table=table,
                    rows_lost=row_count,
                )
                if self._health_tracker:
                    self._health_tracker.record_event("data_loss", table=table, count=row_count)

    def _ch_insert_columnar(
        self,
        table: str,
        column_names: list[str],
        column_data: list[list[Any]],
        row_count: int,
    ):
        """Insert columnar data directly to ClickHouse (CC-1, CC-3).

        Transposes column_data to row-major for clickhouse_connect insert.
        """
        if not column_data or row_count == 0:
            return

        logger.info(f"Inserting {row_count} rows into {table} (Cols: {column_names})")
        start_ms = time.monotonic() * 1000

        # Transpose columnar to row-major for clickhouse_connect
        values = []
        for i in range(row_count):
            values.append([col[i] for col in column_data])

        with self._get_table_lock(table):
            self.ch_client.insert(table, values, column_names=column_names)

        elapsed_ms = time.monotonic() * 1000 - start_ms
        if elapsed_ms > self._insert_warn_ms:
            logger.warning(
                "Slow ClickHouse insert",
                table=table,
                rows=row_count,
                elapsed_ms=round(elapsed_ms, 1),
            )
        logger.info(f"Insert success: {table} {row_count}")

    async def write(self, table: str, data: list):
        """
        Try ClickHouse, fallback to WAL. (Legacy row-dict interface)
        """
        if not data:
            return
        data = self._sanitize_timestamps(table, data)
        if not data:
            return

        success = False
        if self.connected and self.ch_client:
            try:
                async with self._insert_semaphore:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(self._executor, self._ch_insert, table, data)
                success = True
            except (TimeoutError, socket.timeout) as e:
                logger.error(
                    "ClickHouse insert timeout",
                    table=table,
                    timeout_s=self._insert_timeout_s,
                    error=str(e),
                )
                if self._health_tracker:
                    self._health_tracker.record_event("ch_timeout", table=table)
                self._schedule_reconnect("insert_timeout")
                success = False
            except Exception as e:
                logger.error("ClickHouse write failed", table=table, error=str(e))
                if self._health_tracker:
                    self._health_tracker.record_event("ch_error", table=table)
                self._schedule_reconnect("write_failed")
                success = False
        else:
            if self.ch_enabled:
                self._schedule_reconnect("not_connected")

        if not success:
            logger.warning("Fallback to WAL", table=table, count=len(data))
            if self.metrics:
                self.metrics.recorder_wal_writes_total.labels(table=table).inc()
            if self._health_tracker:
                self._health_tracker.record_event("wal_fallback", table=table, count=len(data))

            # CC-4: Use batch writer if available
            batch_writer = self._get_wal_batch_writer()
            if batch_writer is not None:
                wal_ok = await batch_writer.add(table, data)
            else:
                wal_ok = await self.wal.write(table, data)

            if not wal_ok:
                logger.critical(
                    "Data loss: both ClickHouse and WAL failed",
                    table=table,
                    rows_lost=len(data),
                )
                if self._health_tracker:
                    self._health_tracker.record_event("data_loss", table=table, count=len(data))

    def _ch_insert(self, table, data):
        # Infer columns from first row assuming consistent dicts
        if not data:
            return
        logger.info(f"Inserting {len(data)} rows into {table} (Keys: {list(data[0].keys())})")
        start_ms = time.monotonic() * 1000

        keys = list(data[0].keys())
        # Transform data to list of lists (values based on keys order) or dicts if supported
        # clickhouse-connect insert expects list of lists typically
        values = [[row.get(k) for k in keys] for row in data]
        with self._get_table_lock(table):
            self.ch_client.insert(table, values, column_names=keys)

        elapsed_ms = time.monotonic() * 1000 - start_ms
        if elapsed_ms > self._insert_warn_ms:
            logger.warning(
                "Slow ClickHouse insert",
                table=table,
                rows=len(data),
                elapsed_ms=round(elapsed_ms, 1),
            )
        logger.info(f"Insert success: {table} {len(data)}")

    def _sanitize_timestamps(self, table: str, data: list[dict]) -> list[dict]:
        """Drop rows with far-future timestamps and enforce ingest_ts >= exch_ts."""
        if not data:
            return data
        if not self._ts_max_future_ns:
            # Still enforce ingest_ts >= exch_ts when both present
            for row in data:
                try:
                    exch_ts = row.get("exch_ts")
                    ingest_ts = row.get("ingest_ts")
                    if exch_ts and ingest_ts and int(ingest_ts) < int(exch_ts):
                        row["ingest_ts"] = int(exch_ts)
                except Exception:
                    continue
            return data

        now_ns = timebase.now_ns()
        kept: list[dict] = []
        dropped = 0
        for row in data:
            try:
                exch_ts = row.get("exch_ts")
                ingest_ts = row.get("ingest_ts")
                exch_ts_i = int(exch_ts) if exch_ts is not None else 0
                ingest_ts_i = int(ingest_ts) if ingest_ts is not None else 0
                if exch_ts_i and exch_ts_i - now_ns > self._ts_max_future_ns:
                    dropped += 1
                    continue
                if ingest_ts_i and ingest_ts_i - now_ns > self._ts_max_future_ns:
                    dropped += 1
                    continue
                if exch_ts_i and ingest_ts_i and ingest_ts_i < exch_ts_i:
                    row["ingest_ts"] = exch_ts_i
                kept.append(row)
            except Exception:
                kept.append(row)
        if dropped:
            logger.warning(
                "Dropped future timestamp rows",
                table=table,
                dropped=dropped,
                max_future_ns=self._ts_max_future_ns,
            )
        return kept

    def _sanitize_columnar(
        self,
        table: str,
        column_names: list[str],
        column_data: list[list[Any]],
        row_count: int,
    ) -> tuple[list[list[Any]], int]:
        """Columnar variant of _sanitize_timestamps."""
        if row_count == 0:
            return column_data, 0

        try:
            exch_idx = column_names.index("exch_ts")
        except ValueError:
            exch_idx = None
        try:
            ingest_idx = column_names.index("ingest_ts")
        except ValueError:
            ingest_idx = None

        if exch_idx is None and ingest_idx is None:
            return column_data, row_count

        now_ns = timebase.now_ns()
        max_future = self._ts_max_future_ns
        keep_indices: list[int] = []
        dropped = 0

        for i in range(row_count):
            try:
                exch_ts_i = int(column_data[exch_idx][i]) if exch_idx is not None else 0
                ingest_ts_i = int(column_data[ingest_idx][i]) if ingest_idx is not None else 0

                if max_future:
                    if exch_ts_i and exch_ts_i - now_ns > max_future:
                        dropped += 1
                        continue
                    if ingest_ts_i and ingest_ts_i - now_ns > max_future:
                        dropped += 1
                        continue

                if exch_idx is not None and ingest_idx is not None and exch_ts_i and ingest_ts_i:
                    if ingest_ts_i < exch_ts_i:
                        column_data[ingest_idx][i] = exch_ts_i
            except Exception:
                pass

            keep_indices.append(i)

        if dropped:
            logger.warning(
                "Dropped future timestamp rows",
                table=table,
                dropped=dropped,
                max_future_ns=self._ts_max_future_ns,
            )
            if keep_indices:
                column_data = [[col[i] for i in keep_indices] for col in column_data]
            else:
                column_data = []

        return column_data, len(keep_indices)

    async def shutdown(self) -> None:
        """Graceful shutdown: flush WAL batch writer and shutdown thread pool."""
        # Flush WAL batch writer if active
        if self._wal_batch_writer is not None:
            try:
                await self._wal_batch_writer.flush()
            except Exception as e:
                logger.error("WAL batch writer flush failed on shutdown", error=str(e))
            try:
                self._wal_batch_writer.stop()
            except Exception as e:
                logger.error("WAL batch writer stop failed on shutdown", error=str(e))

        # Shutdown thread pool
        self._executor.shutdown(wait=False)

        # Stop heartbeat
        self._heartbeat_running = False
