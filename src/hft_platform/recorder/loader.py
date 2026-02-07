import fcntl
import glob
import json
import os
import shutil
import time
from typing import Any, Dict, List

import clickhouse_connect
from hft_platform.core import timebase
from hft_platform.recorder.schema import apply_schema, ensure_price_scaled_views
from structlog import get_logger

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
            apply_schema(self.ch_client)
            ensure_price_scaled_views(self.ch_client)
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

        logger.info("Starting WAL Loader", wal_dir=self.wal_dir)

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

            time.sleep(self.poll_interval_s)

    def process_files(self):
        # Look for *.jsonl
        # IMPORTANT: Only process files that are NOT currently being written to.
        # Simple heuristic: WALWriter writes to {table}_{timestamp}.jsonl
        # It never appends to old files after rotation.
        # But we must ensure rotation has happened.
        # Generally, we can process files older than X seconds or rely on file locking (not avail here).
        # We will assume WALWriter rotates files and we pick up "stable" ones.

        files = glob.glob(os.path.join(self.wal_dir, "*.jsonl"))
        if not files:
            return

        now = timebase.now_s()
        for fpath in files:
            # Check modification time to ensure writer is done
            mtime = os.path.getmtime(fpath)
            if now - mtime < 2.0:
                # File touched recently, skip
                continue

            fname = os.path.basename(fpath)
            # Extract topic/table name
            # Format: {topic}_{timestamp}.jsonl
            # We can rely on startsWith for known topics
            if fname.startswith("market_data"):
                target_table = "market_data"
            elif fname.startswith("orders"):
                target_table = "orders"
            elif fname.startswith("fills"):
                target_table = "trades"  # Mapping 'fills' topic to 'trades' table
            elif fname.startswith("risk_log"):
                target_table = "risk_log"
            elif fname.startswith("backtest_runs"):
                target_table = "backtest_runs"
            else:
                # Fallback: try to guess by stripping last part
                try:
                    target_table = "_".join(fname.split("_")[:-1])
                except Exception as e:
                    logger.warning("Failed to parse table name from filename", file=fname, error=str(e))
                    target_table = "unknown"

            if target_table == "unknown":
                logger.warning("Unknown table for file", file=fname)
                continue

            logger.info("Loading file", file=fname, table=target_table)

            rows = []
            corrupt_lines = 0
            try:
                with open(fpath, "r") as f:
                    # Try to acquire shared lock (non-blocking)
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        # File is being written, skip for now
                        logger.debug("File locked by writer, skipping", file=fname)
                        continue
                    try:
                        for line in f:
                            try:
                                rows.append(json.loads(line))
                            except json.JSONDecodeError:
                                corrupt_lines += 1
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

                # If entire file is corrupt (all lines failed), quarantine it
                if corrupt_lines > 0 and not rows:
                    self._quarantine_corrupt_file(fpath, fname, f"All {corrupt_lines} lines corrupt")
                    continue
                elif corrupt_lines > 0:
                    logger.warning(
                        "Partial corruption in WAL file",
                        file=fname,
                        corrupt_lines=corrupt_lines,
                        valid_rows=len(rows),
                    )

            except FileNotFoundError:
                # File was moved/deleted between glob and open
                continue

            if rows:
                success = self.insert_batch(target_table, rows)
                if not success:
                    # Move to DLQ instead of archive on failure
                    self._write_to_dlq(target_table, rows, "insert_failed_after_retries")
                    continue

            # Move to archive
            try:
                shutil.move(fpath, os.path.join(self.archive_dir, fname))
                logger.info("Archived file", file=fname)
            except FileNotFoundError:
                pass  # Already moved

    def _quarantine_corrupt_file(self, fpath: str, fname: str, reason: str) -> None:
        """Move corrupt WAL file to quarantine directory."""
        os.makedirs(self.corrupt_dir, exist_ok=True)
        try:
            dest_path = os.path.join(self.corrupt_dir, fname)
            shutil.move(fpath, dest_path)
            logger.error("Moved corrupt WAL to quarantine", file=fname, reason=reason, dest=dest_path)
        except Exception as e:
            logger.error("Failed to quarantine corrupt file", file=fname, error=str(e))

    def _write_to_dlq(self, table: str, rows: List[Dict[str, Any]], error: str) -> None:
        """Write failed rows to Dead Letter Queue for later analysis."""
        os.makedirs(self.dlq_dir, exist_ok=True)
        ts = int(timebase.now_ns())
        dlq_file = os.path.join(self.dlq_dir, f"{table}_{ts}.jsonl")
        try:
            with open(dlq_file, "w") as f:
                # Write metadata header
                f.write(
                    json.dumps(
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
                    f.write(json.dumps(row) + "\n")
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

            if self.ch_client and data:
                last_error = None
                for attempt in range(self._insert_max_retries):
                    try:
                        self.ch_client.insert("hft.market_data", data, column_names=cols)
                        logger.info("Inserted batch", table=table, count=len(rows))
                        return True
                    except Exception as e:
                        last_error = e
                        if attempt < self._insert_max_retries - 1:
                            delay = self._compute_insert_backoff(attempt)
                            logger.warning(
                                "Insert failed, retrying with backoff",
                                table=table,
                                attempt=attempt + 1,
                                delay_s=round(delay, 2),
                                error=str(e),
                            )
                            time.sleep(delay)
                        else:
                            logger.error(
                                "Insert failed after max retries",
                                table=table,
                                max_retries=self._insert_max_retries,
                                error=str(last_error),
                            )
                            return False
            elif data:
                # No client but we have data - also a "failure" for DLQ purposes
                logger.warning("No ClickHouse client available for insert", table=table, count=len(data))
                return False

        logger.info("Inserted batch", table=table, count=len(rows))
        return True


if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging

    configure_logging()
    loader = WALLoaderService()
    loader.run()
