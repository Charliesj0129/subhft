import asyncio
import os
import random
import socket
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.infra.ch_client import get_ch_config
from hft_platform.recorder.schema import apply_schema, ensure_price_scaled_views
from hft_platform.recorder.wal import WALWriter

try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect = None  # type: ignore[assignment]

logger = get_logger("recorder.writer")


class WriterDoubleFaultError(Exception):
    """Both ClickHouse and WAL failed — raised to signal batcher of data loss risk."""

    pass


class L7PartialMigrationError(RuntimeError):
    """Raised when only one of the two L7 audit-column migrations is applied.

    The L7 dual-write contract requires both ``20260505_001`` (orders) and
    ``20260505_002`` (fills) to be applied together OR neither to be applied.
    A half-applied state is unsafe because the recorder cannot decide whether
    to emit audit columns for hft.fills if hft.orders accepted them — and
    vice versa. Operator must complete or roll back the missing half before
    the recorder will start.
    """

    pass


_L7_AUDIT_COLUMN_NAMES: frozenset[str] = frozenset(
    {
        "trace_id",
        "feature_snapshot_id",
        "risk_decision_id",
        "strategy_version",
        "config_hash",
        "git_sha",
        "data_session_id",
    }
)

_L7_AUDIT_TABLES: frozenset[str] = frozenset({"hft.orders", "hft.fills"})

_L7_AUDIT_MIGRATION_VERSIONS: tuple[str, str] = ("20260505_001", "20260505_002")


class DataWriter:
    DEFAULT_NATIVE_PORT = 9000
    DEFAULT_HTTP_PORT = 8123

    DEFAULT_MAX_RETRIES = 5
    DEFAULT_BASE_DELAY_S = 1.0
    DEFAULT_MAX_BACKOFF_S = 30.0
    DEFAULT_JITTER_FACTOR = 0.5
    _NATIVE_INTERFACE_ERROR_PATTERNS = (
        "unrecognized client type native",
        "invalid interface",
    )

    def __init__(self, ch_host="localhost", ch_port=9000, wal_dir=".wal"):
        self.ch_client = None
        self.wal = WALWriter(wal_dir)
        self._ch_locks: dict[str, threading.Lock] = {}
        self._ch_locks_guard = threading.Lock()
        self._ch_heartbeat_lock = threading.Lock()
        use_native = ch_port == self.DEFAULT_NATIVE_PORT
        _cfg = get_ch_config()
        self.ch_params = {
            "host": ch_host,
            "port": ch_port,
            "username": _cfg["username"],
            "password": _cfg["password"],
            "compress": True,
        }
        if use_native:
            self.ch_params["interface"] = "native"
        self.connected = False
        self._schema_initialized = False
        self.ch_enabled = str(os.getenv("HFT_CLICKHOUSE_ENABLED", "")).lower() in ("1", "true", "yes", "on")
        if os.getenv("HFT_DISABLE_CLICKHOUSE"):
            warnings.warn(
                "HFT_DISABLE_CLICKHOUSE is deprecated, use HFT_CLICKHOUSE_ENABLED=0 instead",
                DeprecationWarning,
                stacklevel=2,
            )
            logger.warning("Deprecated env var HFT_DISABLE_CLICKHOUSE used; migrate to HFT_CLICKHOUSE_ENABLED=0")
            self.ch_enabled = False
        self.ch_params["host"] = os.getenv("HFT_CLICKHOUSE_HOST", self.ch_params["host"])
        env_port = os.getenv("HFT_CLICKHOUSE_PORT")
        if env_port:
            self.ch_params["port"] = int(env_port)
            if int(env_port) == self.DEFAULT_NATIVE_PORT:
                self.ch_params["interface"] = "native"
            elif "interface" in self.ch_params:
                del self.ch_params["interface"]

        self._insert_timeout_s = float(os.getenv("HFT_CH_INSERT_TIMEOUT_S", "30"))
        self._insert_warn_ms = int(os.getenv("HFT_CH_INSERT_WARN_MS", "5000"))
        self._ch_column_oriented = os.getenv("HFT_CH_COLUMN_ORIENTED", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._ch_insert_chunk_rows = int(os.getenv("HFT_CH_INSERT_CHUNK_ROWS", "0") or "0")
        self._ch_insert_log_every = int(os.getenv("HFT_CH_INSERT_LOG_EVERY", "0") or "0")
        self._ch_insert_log_success = os.getenv("HFT_CH_INSERT_LOG_SUCCESS", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._ch_insert_count = 0
        self._ch_columnar_insert_count = 0
        self._native_interface_fallback_used = False
        self.ch_params["send_receive_timeout"] = int(self._insert_timeout_s)

        self._max_concurrent_inserts = int(os.getenv("HFT_CH_MAX_CONCURRENT_INSERTS", "6"))
        self._pool_size = max(1, int(os.getenv("HFT_CH_INSERT_POOL_SIZE", str(self._max_concurrent_inserts))))
        if self._pool_size > self._max_concurrent_inserts:
            logger.debug(
                "ch_pool_size_exceeds_semaphore_ceiling_reducing",
                pool_size=self._pool_size,
                semaphore_ceiling=self._max_concurrent_inserts,
            )
            self._pool_size = self._max_concurrent_inserts
        self._executor = ThreadPoolExecutor(
            max_workers=self._pool_size,
            thread_name_prefix="ch-insert",
        )
        self._insert_semaphore = asyncio.Semaphore(self._max_concurrent_inserts)

        self._wal_batch_writer: Any = None
        self._wal_batch_writer_init_lock = threading.Lock()
        self._wal_batch_enabled = os.getenv("HFT_WAL_BATCH_ENABLED", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        self._health_tracker: Any = None

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
        except Exception as _exc:
            self.metrics = None

        self._l7_audit_active_tables: set[str] = set()
        self._l7_audit_detected: bool = False

    @classmethod
    def _is_native_interface_unsupported_error(cls, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(pat in text for pat in cls._NATIVE_INTERFACE_ERROR_PATTERNS)

    def _maybe_fallback_clickhouse_interface(self, exc: Exception) -> bool:
        """Downgrade to HTTP params if clickhouse_connect build lacks native interface support."""
        if self.ch_params.get("interface") != "native":
            return False
        if not self._is_native_interface_unsupported_error(exc):
            return False

        prev_port = int(self.ch_params.get("port", self.DEFAULT_NATIVE_PORT))
        fallback_port = int(os.getenv("HFT_CLICKHOUSE_HTTP_FALLBACK_PORT", str(self.DEFAULT_HTTP_PORT)))
        self.ch_params.pop("interface", None)
        if prev_port == self.DEFAULT_NATIVE_PORT:
            self.ch_params["port"] = fallback_port
        self._native_interface_fallback_used = True
        logger.warning(
            "ClickHouse native interface unsupported by client, falling back",
            previous_port=prev_port,
            fallback_port=self.ch_params.get("port"),
            interface="http",
        )
        return True

    def _create_clickhouse_client(self):
        try:
            return clickhouse_connect.get_client(**self.ch_params)
        except Exception as e:
            if self._maybe_fallback_clickhouse_interface(e):
                return clickhouse_connect.get_client(**self.ch_params)
            raise

    def _should_log_insert_success(self, counter: int) -> bool:
        if self._ch_insert_log_success:
            return True
        return self._ch_insert_log_every > 0 and counter % self._ch_insert_log_every == 0

    def _iter_row_chunks(self, data: list[Any]) -> list[list[Any]]:
        if not self._ch_insert_chunk_rows or len(data) <= self._ch_insert_chunk_rows:
            return [data]
        chunk = self._ch_insert_chunk_rows
        return [data[i : i + chunk] for i in range(0, len(data), chunk)]

    def _iter_columnar_chunks(
        self,
        column_data: list[list[Any]],
        row_count: int,
    ) -> list[tuple[list[list[Any]], int]]:
        if not self._ch_insert_chunk_rows or row_count <= self._ch_insert_chunk_rows:
            return [(column_data, row_count)]
        chunk = self._ch_insert_chunk_rows
        chunks: list[tuple[list[list[Any]], int]] = []
        for start in range(0, row_count, chunk):
            end = min(row_count, start + chunk)
            chunks.append(([col[start:end] for col in column_data], end - start))
        return chunks

    def set_health_tracker(self, tracker: Any) -> None:
        self._health_tracker = tracker

    def _get_wal_batch_writer(self) -> Any:
        """Lazy-init WAL batch writer (CC-4).

        M2 (2026-04-25): double-checked locking prevents two threads from
        creating racing WALBatchWriter instances. Each WALBatchWriter spawns
        its own ``wal-batch-timer`` daemon thread; without the lock a race
        could leak background timers (memory + duplicate flush traffic).
        Hot path is the un-locked first-read; the lock is only entered on
        the very first call from the first batcher in a process lifetime.
        """
        if not self._wal_batch_enabled:
            return None
        writer = self._wal_batch_writer
        if writer is not None:
            return writer
        with self._wal_batch_writer_init_lock:
            if self._wal_batch_writer is None:
                try:
                    from hft_platform.recorder.wal import WALBatchWriter

                    self._wal_batch_writer = WALBatchWriter(self.wal.wal_dir)
                except Exception as _exc:
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
        delay = min(self._base_delay_s * (2**attempt), self._max_backoff_s)
        jitter = delay * self._jitter_factor * (random.random() * 2 - 1)
        return max(0.1, delay + jitter)

    def connect(self):
        """Synchronous connect - use connect_async() in async contexts."""
        if not self.ch_enabled or not clickhouse_connect:
            logger.info("Running in WAL-only mode (ClickHouse disabled or driver missing)")
            return

        for attempt in range(self._max_retries):
            self._connect_attempts = attempt
            try:
                self.ch_client = self._create_clickhouse_client()
                self.connected = True
                self._connect_attempts = 0
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
                self.ch_client = await asyncio.to_thread(self._create_clickhouse_client)
                self.connected = True
                self._connect_attempts = 0
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
            if self.metrics:
                try:
                    self.metrics.recorder_schema_init_failed.set(0)
                except Exception:
                    pass
        except Exception as se:
            logger.critical(
                "Schema initialization failed - falling back to WAL-only mode",
                error=str(se),
                exc_info=True,
            )
            self._schema_initialized = False
            self.connected = False
            if self.metrics:
                try:
                    self.metrics.recorder_schema_init_failed.set(1)
                except Exception:
                    pass
            return

        self._detect_l7_audit_columns()

        try:
            ensure_price_scaled_views(self.ch_client)
        except Exception as se:
            logger.error("Schema view repair failed", error=str(se))

    def _detect_l7_audit_columns(self) -> None:
        """Query hft.schema_migrations to populate ``_l7_audit_active_tables``.

        Mapping (migration_version → table):
          * ``20260505_001`` → ``hft.orders``
          * ``20260505_002`` → ``hft.fills``

        Behavior matrix:
          * Both versions present → both tables active (full L7 mode).
          * Neither present → no tables active (legacy mode; strip audit cols).
          * Exactly one present → raise ``L7PartialMigrationError``.
          * Query failure → log + leave detected=False so insert path strips.
        """
        if self.ch_client is None:
            return
        try:
            v_orders, v_fills = _L7_AUDIT_MIGRATION_VERSIONS
            result = self.ch_client.query(
                f"SELECT version FROM hft.schema_migrations WHERE version IN ('{v_orders}', '{v_fills}')"  # nosec B608
            )
            applied = {row[0] for row in result.result_rows}
            orders_applied = v_orders in applied
            fills_applied = v_fills in applied

            if orders_applied != fills_applied:
                missing = v_orders if not orders_applied else v_fills
                applied_one = v_orders if orders_applied else v_fills
                raise L7PartialMigrationError(
                    f"L7 audit-column migration partial state: applied={applied_one}, "
                    f"missing={missing}. Either complete the missing migration "
                    "(re-run apply_schema with the file present) or roll back the "
                    "applied one. Refusing to start the writer in partial state."
                )

            active: set[str] = set()
            if orders_applied:
                active.add("hft.orders")
            if fills_applied:
                active.add("hft.fills")
            self._l7_audit_active_tables = active
            self._l7_audit_detected = True
            logger.info(
                "l7_audit_columns_detected",
                active_tables=sorted(active),
                mode="extended" if active else "legacy",
            )
        except L7PartialMigrationError:
            raise
        except Exception as exc:
            logger.warning(
                "l7_audit_columns_detection_failed_defaulting_legacy",
                error=str(exc),
            )
            self._l7_audit_active_tables = set()
            self._l7_audit_detected = False

    def _l7_should_strip(self, table: str) -> bool:
        """True if audit columns must be stripped before INSERT into ``table``.

        Tables outside ``_L7_AUDIT_TABLES`` always pass through (False).
        Audited tables strip when the migration is not applied for that table
        (legacy mode) or when detection has not yet run.
        """
        if table not in _L7_AUDIT_TABLES:
            return False
        return table not in self._l7_audit_active_tables

    def _strip_l7_columnar(
        self,
        table: str,
        column_names: list[str],
        column_data: list[list[Any]],
    ) -> tuple[list[str], list[list[Any]]]:
        """Drop L7 audit columns from a columnar payload when destination is legacy.

        No-op for tables outside ``_L7_AUDIT_TABLES``. Returns a new (names, data)
        pair without mutating the inputs so the caller's references stay valid
        for retry/WAL fallback.
        """
        if not self._l7_should_strip(table):
            return column_names, column_data
        keep_idx = [i for i, n in enumerate(column_names) if n not in _L7_AUDIT_COLUMN_NAMES]
        if len(keep_idx) == len(column_names):
            return column_names, column_data
        return [column_names[i] for i in keep_idx], [column_data[i] for i in keep_idx]

    def _strip_l7_rowdicts(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop L7 audit keys from each row dict when destination is legacy.

        No-op for tables outside ``_L7_AUDIT_TABLES``. Returns a fresh list of
        dicts to avoid mutating shared references.
        """
        if not self._l7_should_strip(table):
            return rows
        return [{k: v for k, v in row.items() if k not in _L7_AUDIT_COLUMN_NAMES} for row in rows]

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
                while self._heartbeat_running and self.connected:
                    time.sleep(self._heartbeat_interval_s)
                    if not self._heartbeat_running:
                        break
                    ok = self._do_heartbeat_check()
                    self._last_heartbeat_ts = timebase.now_s()
                    self._last_heartbeat_ok = ok
                    if not ok:
                        logger.error("ClickHouse heartbeat failed, marking connection as stale")
                        with self._ch_heartbeat_lock:
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
        try:
            with self._ch_heartbeat_lock:
                if not self.ch_client:
                    return False
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
            "ch_interface": self.ch_params.get("interface", "http"),
            "native_interface_fallback_used": self._native_interface_fallback_used,
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

            batch_writer = self._get_wal_batch_writer()
            if batch_writer is not None and hasattr(batch_writer, "add_columnar"):
                wal_ok = await batch_writer.add_columnar(table, column_names, column_data, row_count)
            elif batch_writer is not None:
                row_dicts = self._columnar_to_row_dicts(column_names, column_data, row_count)
                wal_ok = await batch_writer.add(table, row_dicts)
            else:
                row_dicts = self._columnar_to_row_dicts(column_names, column_data, row_count)
                wal_ok = await self.wal.write(table, row_dicts)

            if not wal_ok:
                logger.critical(
                    "Data loss: both ClickHouse and WAL failed",
                    table=table,
                    rows_lost=row_count,
                )
                if self._health_tracker:
                    self._health_tracker.record_event("data_loss", table=table, count=row_count)
                raise WriterDoubleFaultError(f"Both CH and WAL failed for table={table}, rows={row_count}")

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

        for chunk_data, chunk_rows in self._iter_columnar_chunks(column_data, row_count):
            self._ch_insert_columnar_once(table, column_names, chunk_data, chunk_rows)

    def _ch_insert_columnar_once(
        self,
        table: str,
        column_names: list[str],
        column_data: list[list[Any]],
        row_count: int,
    ) -> None:
        column_names, column_data = self._strip_l7_columnar(table, column_names, column_data)
        self._ch_columnar_insert_count += 1
        if self._should_log_insert_success(self._ch_columnar_insert_count):
            logger.info("ClickHouse columnar insert start", table=table, rows=row_count)
        start_ms = time.monotonic() * 1000
        with self._ch_heartbeat_lock:
            if self.ch_client is None:
                raise ConnectionError("ClickHouse client disconnected during insert")
            with self._get_table_lock(table):
                if self._ch_column_oriented:
                    try:
                        self.ch_client.insert(
                            table,
                            column_data,
                            column_names=column_names,
                            column_oriented=True,
                        )
                    except TypeError as te:
                        self._ch_column_oriented = False
                        logger.debug(
                            "column_oriented_fallback",
                            table=table,
                            error=str(te),
                        )
                        values = self._transpose_columnar_rows(column_data, row_count)
                        self.ch_client.insert(table, values, column_names=column_names)
                else:
                    values = self._transpose_columnar_rows(column_data, row_count)
                    self.ch_client.insert(table, values, column_names=column_names)
        elapsed_ms = time.monotonic() * 1000 - start_ms
        try:
            if self.metrics:
                self.metrics.recorder_ch_insert_latency_ms.labels(table=table).observe(elapsed_ms)
        except Exception:
            pass
        if elapsed_ms > self._insert_warn_ms:
            logger.warning(
                "Slow ClickHouse insert",
                table=table,
                rows=row_count,
                elapsed_ms=round(elapsed_ms, 1),
                mode="columnar",
            )
        elif self._should_log_insert_success(self._ch_columnar_insert_count):
            logger.info(
                "ClickHouse columnar insert success", table=table, rows=row_count, elapsed_ms=round(elapsed_ms, 1)
            )

    @staticmethod
    def _transpose_columnar_rows(column_data: list[list[Any]], row_count: int) -> list[list[Any]]:
        if row_count <= 0 or not column_data:
            return []
        return [list(row) for row in zip(*column_data)]

    @staticmethod
    def _columnar_to_row_dicts(
        column_names: list[str],
        column_data: list[list[Any]],
        row_count: int,
    ) -> list[dict[str, Any]]:
        if row_count <= 0 or not column_names or not column_data:
            return []
        names = tuple(column_names)
        return [dict(zip(names, row, strict=False)) for row in zip(*column_data)]

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
                raise WriterDoubleFaultError(f"Both CH and WAL failed for table={table}, rows={len(data)}")

    def _ch_insert(self, table, data):
        if not data:
            return
        for chunk in self._iter_row_chunks(data):
            self._ch_insert_once(table, chunk)

    def _ch_insert_once(self, table: str, data: list[dict[str, Any]]) -> None:
        data = self._strip_l7_rowdicts(table, data)
        self._ch_insert_count += 1
        if self._should_log_insert_success(self._ch_insert_count):
            logger.info("ClickHouse row insert start", table=table, rows=len(data))
        start_ms = time.monotonic() * 1000

        keys = list(data[0].keys())
        values = [[row.get(k) for k in keys] for row in data]
        with self._ch_heartbeat_lock:
            if self.ch_client is None:
                raise ConnectionError("ClickHouse client disconnected during insert")
            with self._get_table_lock(table):
                self.ch_client.insert(table, values, column_names=keys)

        elapsed_ms = time.monotonic() * 1000 - start_ms
        try:
            if self.metrics:
                self.metrics.recorder_ch_insert_latency_ms.labels(table=table).observe(elapsed_ms)
        except Exception:
            pass
        if elapsed_ms > self._insert_warn_ms:
            logger.warning(
                "Slow ClickHouse insert",
                table=table,
                rows=len(data),
                elapsed_ms=round(elapsed_ms, 1),
                mode="row",
            )
        elif self._should_log_insert_success(self._ch_insert_count):
            logger.info("ClickHouse row insert success", table=table, rows=len(data), elapsed_ms=round(elapsed_ms, 1))

    def _sanitize_timestamps(self, table: str, data: list[dict]) -> list[dict]:
        """Drop rows with far-future timestamps and enforce ingest_ts >= exch_ts."""
        if not data:
            return data
        if not self._ts_max_future_ns:
            result: list[dict] = []
            for row in data:
                try:
                    exch_ts = row.get("exch_ts")
                    ingest_ts = row.get("ingest_ts")
                    if exch_ts and ingest_ts and int(ingest_ts) < int(exch_ts):
                        row = dict(row)
                        row["ingest_ts"] = int(exch_ts)
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                finally:
                    result.append(row)
            return result

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
                    row = dict(row)
                    row["ingest_ts"] = exch_ts_i
                kept.append(row)
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
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

        if ingest_idx is not None:
            column_data[ingest_idx] = list(column_data[ingest_idx])

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
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
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
        shutdown_timeout = int(os.getenv("HFT_CH_SHUTDOWN_TIMEOUT_S", "30"))

        self._heartbeat_running = False

        if self._wal_batch_writer is not None:
            try:
                await self._wal_batch_writer.flush()
            except Exception as e:
                logger.error("WAL batch writer flush failed on shutdown", error=str(e))
            try:
                self._wal_batch_writer.stop()
            except Exception as e:
                logger.error("WAL batch writer stop failed on shutdown", error=str(e))

        deadline = time.monotonic() + shutdown_timeout
        drained = True
        while self._insert_semaphore._value < self._max_concurrent_inserts:
            if time.monotonic() > deadline:
                remaining = self._max_concurrent_inserts - self._insert_semaphore._value
                logger.warning(
                    "Shutdown timeout — in-flight inserts may be lost",
                    remaining_inserts=remaining,
                    timeout_s=shutdown_timeout,
                )
                drained = False
                break
            await asyncio.sleep(0.1)

        if drained:
            logger.info("All in-flight inserts drained before shutdown")

        self._executor.shutdown(wait=drained)
