import asyncio
import os
from typing import Any

from structlog import get_logger

from hft_platform.recorder.wal import WALWriter

# import clickhouse_connect # Mocked for now if not available in env
try:
    import clickhouse_connect
except ImportError:
    clickhouse_connect: Any | None = None

logger = get_logger("recorder.writer")


class DataWriter:
    def __init__(self, ch_host="localhost", ch_port=8123, wal_dir=".wal"):
        self.ch_client = None
        self.wal = WALWriter(wal_dir)
        self.ch_params = {"host": ch_host, "port": ch_port, "username": "default", "password": ""}
        self.connected = False
        # ClickHouse is opt-in; enable by setting HFT_CLICKHOUSE_ENABLED=1
        self.ch_enabled = str(os.getenv("HFT_CLICKHOUSE_ENABLED", "")).lower() in ("1", "true", "yes", "on")
        if os.getenv("HFT_DISABLE_CLICKHOUSE"):
            self.ch_enabled = False
        # Allow host/port override via env
        self.ch_params["host"] = os.getenv("HFT_CLICKHOUSE_HOST", self.ch_params["host"])
        self.ch_params["port"] = int(os.getenv("HFT_CLICKHOUSE_PORT", self.ch_params["port"]))

    def connect(self):
        if not self.ch_enabled or not clickhouse_connect:
            logger.info("Running in WAL-only mode (ClickHouse disabled or driver missing)")
            return

        import time

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.ch_client = clickhouse_connect.get_client(**self.ch_params)
                self.connected = True
                logger.info("Connected to ClickHouse")

                # Auto-Init Schema
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
                
                # If success, break loop
                break

            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        "ClickHouse connection failed, retrying...",
                        error=str(e),
                        attempt=attempt + 1,
                    )
                    time.sleep(2)
                else:
                    logger.warning("ClickHouse connection failed, falling back to WAL", error=str(e))
                    self.connected = False

    async def write(self, table: str, data: list):
        """
        Try ClickHouse, fallback to WAL.
        """
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
