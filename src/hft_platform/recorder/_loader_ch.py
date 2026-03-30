"""ClickHouse connection, insert-with-retry, and dedup helpers.

All functions operate on a ``WALLoaderService`` instance passed as the
first argument so that the public API surface stays in ``loader.py``.
"""

from __future__ import annotations

import os
import random
import time
import warnings
from typing import Any

import clickhouse_connect

from hft_platform.recorder._loader_common import (
    logger,
    timebase,
)
from hft_platform.recorder.schema import apply_schema, ensure_price_scaled_views

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def connect(svc: Any) -> None:
    """Establish a ClickHouse connection and ensure schema exists."""
    try:
        ch_username = os.getenv("HFT_CLICKHOUSE_USER")
        if not ch_username and os.getenv("HFT_CLICKHOUSE_USERNAME"):
            ch_username = os.getenv("HFT_CLICKHOUSE_USERNAME")
            warnings.warn(
                "HFT_CLICKHOUSE_USERNAME is deprecated, use HFT_CLICKHOUSE_USER instead",
                DeprecationWarning,
                stacklevel=2,
            )
            logger.warning("Deprecated env var HFT_CLICKHOUSE_USERNAME used; migrate to HFT_CLICKHOUSE_USER")
        if not ch_username and os.getenv("CLICKHOUSE_USER"):
            ch_username = os.getenv("CLICKHOUSE_USER")
        # TODO(2026-Q3): remove CLICKHOUSE_USERNAME fallback — deprecated since 2026-03
        if not ch_username and os.getenv("CLICKHOUSE_USERNAME"):
            ch_username = os.getenv("CLICKHOUSE_USERNAME")
            warnings.warn(
                "CLICKHOUSE_USERNAME is deprecated, use HFT_CLICKHOUSE_USER instead",
                DeprecationWarning,
                stacklevel=2,
            )
            logger.warning("Deprecated env var CLICKHOUSE_USERNAME used; migrate to HFT_CLICKHOUSE_USER")
        if not ch_username:
            ch_username = "default"
        ch_password = os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD") or ""
        svc.ch_client = clickhouse_connect.get_client(
            host=svc.ch_host,
            port=svc.ch_port,
            username=ch_username,
            password=ch_password,
        )
        try:
            apply_schema(svc.ch_client)
        except Exception as e:
            logger.error("Schema initialization failed", error=str(e))
        try:
            ensure_price_scaled_views(svc.ch_client)
        except Exception as e:
            logger.error("Schema view repair failed", error=str(e))
        logger.info("Connected to ClickHouse and ensured schema.")
    except ConnectionError as e:
        logger.error(
            "Connection refused by ClickHouse",
            error=str(e),
            host=svc.ch_host,
            port=svc.ch_port,
        )
        svc.ch_client = None
    except TimeoutError as e:
        logger.error(
            "Connection timeout to ClickHouse",
            error=str(e),
            host=svc.ch_host,
            port=svc.ch_port,
        )
        svc.ch_client = None
    except FileNotFoundError as e:
        logger.error("Schema file not found", error=str(e))
    except Exception as e:
        logger.error(
            "Failed to connect to ClickHouse",
            error=str(e),
            error_type=type(e).__name__,
        )
        svc.ch_client = None


def compute_connect_backoff(svc: Any, attempt: int) -> float:
    """Compute exponential backoff delay for connection retry."""
    delay = min(svc._connect_base_delay_s * (2**attempt), svc._connect_max_backoff_s)
    jitter = delay * 0.25 * (random.random() * 2 - 1)
    return max(1.0, delay + jitter)


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------


def compute_insert_backoff(svc: Any, attempt: int) -> float:
    """Compute backoff delay for insert retry."""
    delay = min(svc._insert_base_delay_s * (2**attempt), svc._insert_max_backoff_s)
    jitter = delay * 0.25 * (random.random() * 2 - 1)
    return max(0.1, delay + jitter)


def insert_with_retry(
    svc: Any,
    full_table_name: str,
    cols: list,
    data: list,
    table_alias: str,
    row_count: int,
) -> bool:
    """Insert *data* into ClickHouse with retry logic.

    Returns ``True`` on success, ``False`` if all retries failed.
    """
    if not data:
        return True

    if svc.ch_client and data:
        last_error = None
        for attempt in range(svc._insert_max_retries):
            try:
                with svc._ch_lock:
                    svc.ch_client.insert(full_table_name, data, column_names=cols)
                logger.info("Inserted batch", table=table_alias, count=row_count)
                if svc.metrics:
                    if hasattr(svc.metrics, "recorder_insert_batches_total"):
                        outcome = "success_no_retry" if attempt == 0 else "success_after_retry"
                        svc.metrics.recorder_insert_batches_total.labels(table=table_alias, result=outcome).inc()
                    if attempt > 0:
                        svc.metrics.recorder_insert_retry_total.labels(table=table_alias, result="success").inc()
                    svc.metrics.wal_replay_throughput_rows_total.inc(row_count)
                return True
            except Exception as e:
                last_error = e
                if attempt < svc._insert_max_retries - 1:
                    if svc.metrics:
                        svc.metrics.recorder_insert_retry_total.labels(table=table_alias, result="retry").inc()
                    delay = compute_insert_backoff(svc, attempt)
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
                        max_retries=svc._insert_max_retries,
                        error=str(last_error),
                    )
                    if svc.metrics:
                        if hasattr(svc.metrics, "recorder_insert_batches_total"):
                            svc.metrics.recorder_insert_batches_total.labels(
                                table=table_alias, result="failed_after_retry"
                            ).inc()
                        svc.metrics.recorder_insert_retry_total.labels(table=table_alias, result="failed").inc()
                        svc.metrics.wal_replay_errors_total.labels(type="insert_failed").inc()
                    return False
    elif data:
        logger.warning(
            "No ClickHouse client available for insert",
            table=table_alias,
            count=len(data),
        )
        if svc.metrics:
            if hasattr(svc.metrics, "recorder_insert_batches_total"):
                svc.metrics.recorder_insert_batches_total.labels(table=table_alias, result="failed_no_client").inc()
            svc.metrics.wal_replay_errors_total.labels(type="no_client").inc()
        return False

    return True


# ---------------------------------------------------------------------------
# Dedup helpers (EC-1)
# ---------------------------------------------------------------------------


def is_duplicate(svc: Any, table: str, content_hash: str) -> bool:
    """Check if WAL content hash was already inserted."""
    try:
        with svc._ch_lock:
            result = svc.ch_client.command(
                "SELECT count() FROM hft._wal_dedup WHERE table = %(table)s AND hash = %(hash)s",
                parameters={"table": table, "hash": content_hash},
            )
        return int(result) > 0
    except Exception as _exc:  # noqa: BLE001
        return False


def record_dedup(svc: Any, table: str, content_hash: str, row_count: int) -> None:
    """Record WAL content hash after successful insert."""
    try:
        with svc._ch_lock:
            svc.ch_client.insert(
                "hft._wal_dedup",
                [[table, content_hash, row_count, timebase.now_ns()]],
                column_names=["table", "hash", "row_count", "ts"],
            )
    except Exception as e:
        logger.warning("Failed to record dedup hash", error=str(e))


def insert_with_dedup(svc: Any, target_table: str, rows: list, fname: str) -> bool:
    """Insert rows with optional dedup guard (EC-1).

    Returns True on success.
    """
    if not rows:
        return True

    if svc._dedup_enabled and svc.ch_client:
        import hashlib

        raw = "".join(str(r) for r in rows)
        content_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        if svc._is_duplicate(target_table, content_hash):
            logger.info(
                "Skipping duplicate WAL batch",
                file=fname,
                table=target_table,
                hash=content_hash,
            )
            return True
        success = svc.insert_batch(target_table, rows)
        if success:
            svc._record_dedup(target_table, content_hash, len(rows))
        return success
    else:
        return svc.insert_batch(target_table, rows)
