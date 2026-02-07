import asyncio
import os
import random
import time

from structlog import get_logger

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

        import time

        for attempt in range(self._max_retries):
            self._connect_attempts = attempt
            try:
                self.ch_client = clickhouse_connect.get_client(**self.ch_params)
                self.connected = True
                self._connect_attempts = 0  # Reset on success
                logger.info("Connected to ClickHouse", attempt=attempt + 1)
                self._init_schema()
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
                await asyncio.to_thread(self._init_schema)
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

    def _init_schema(self):
        """Initialize ClickHouse schema from SQL file."""
        try:
            schema_path = os.path.join(os.path.dirname(__file__), "../schemas/clickhouse.sql")
            if os.path.exists(schema_path):
                with open(schema_path, "r") as f:
                    sql_script = f.read()
                    statements = sql_script.split(";")
                    for stmt in statements:
                        if stmt.strip():
                            self.ch_client.command(stmt)
                logger.info("Schema initialized from SQL")
            else:
                logger.warning("Schema file not found", path=schema_path)
        except Exception as se:
            logger.error("Schema initialization failed", error=str(se))

    async def write(self, table: str, data: list):
        """
        Try ClickHouse, fallback to WAL.
        """
        if not data:
            return
        data = self._sanitize_timestamps(table, data)
        if not data:
            return

        success = False
        if self.connected and self.ch_client:
            try:
                # Run sync client in executor if needed, but for batch inserts it's okay-ish
                # or use asyncio.to_thread
                await asyncio.to_thread(self._ch_insert, table, data)
                success = True
            except Exception as e:
                logger.error("ClickHouse write failed", table=table, error=str(e))
                success = False

        if not success:
            logger.warning("Fallback to WAL", table=table, count=len(data))
            # WAL write is now async
            await self.wal.write(table, data)

    def _ch_insert(self, table, data):
        # Infer columns from first row assuming consistent dicts
        if not data:
            return
        logger.info(f"Inserting {len(data)} rows into {table} (Keys: {list(data[0].keys())})")
        keys = list(data[0].keys())
        # Transform data to list of lists (values based on keys order) or dicts if supported
        # clickhouse-connect insert expects list of lists typically
        values = [[row.get(k) for k in keys] for row in data]
        self.ch_client.insert(table, values, column_names=keys)
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

        now_ns = time.time_ns()
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
