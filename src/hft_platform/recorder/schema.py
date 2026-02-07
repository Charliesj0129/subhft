from __future__ import annotations

import os
import re
from typing import Iterable

from structlog import get_logger

logger = get_logger("recorder.schema")

DEFAULT_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "../schemas/clickhouse.sql")


def _load_statements(schema_path: str = DEFAULT_SCHEMA_PATH) -> list[str]:
    if not os.path.exists(schema_path):
        return []
    with open(schema_path, "r") as f:
        sql_script = f.read()
    return [stmt.strip() for stmt in sql_script.split(";") if stmt.strip()]


def apply_schema(client, schema_path: str = DEFAULT_SCHEMA_PATH) -> None:
    statements = _load_statements(schema_path)
    if not statements:
        logger.warning("Schema file not found", path=schema_path)
        return
    for stmt in statements:
        client.command(stmt)
    logger.info("Schema initialized from SQL")


def _view_uses_legacy_price(client, name: str) -> bool:
    try:
        result = client.query(
            "SELECT create_table_query FROM system.tables WHERE database='hft' AND name=%(name)s",
            parameters={"name": name},
        )
        if not result.result_rows:
            return False
        query = result.result_rows[0][0] or ""
    except Exception as exc:
        logger.warning("Failed to inspect view definition", name=name, error=str(exc))
        return False

    has_price = bool(re.search(r"\\bprice\\b", query))
    has_price_scaled = "price_scaled" in query
    return has_price and not has_price_scaled


def _execute_all(client, statements: Iterable[str]) -> None:
    for stmt in statements:
        client.command(stmt)


def ensure_price_scaled_views(client) -> bool:
    """Repair legacy views that still reference `price` instead of `price_scaled`."""
    if not _view_uses_legacy_price(client, "candles_1m_mv"):
        return False

    logger.warning("Legacy candles_1m_mv detected, repairing view definitions")
    _execute_all(
        client,
        [
            "CREATE DATABASE IF NOT EXISTS hft",
            "DROP TABLE IF EXISTS hft.candles_1m_mv",
            "DROP TABLE IF EXISTS hft.ohlcv_1m_mv",
            """
            CREATE TABLE IF NOT EXISTS hft.ohlcv_1m (
                symbol String,
                exchange String,
                bucket DateTime Codec(DoubleDelta, LZ4),
                open_scaled Int64 Codec(DoubleDelta, LZ4),
                high_scaled Int64 Codec(DoubleDelta, LZ4),
                low_scaled Int64 Codec(DoubleDelta, LZ4),
                close_scaled Int64 Codec(DoubleDelta, LZ4),
                volume Int64 Codec(DoubleDelta, LZ4),
                tick_count UInt64
            ) ENGINE = SummingMergeTree()
            PARTITION BY toYYYYMM(bucket)
            ORDER BY (symbol, bucket)
            """,
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS hft.ohlcv_1m_mv
            TO hft.ohlcv_1m AS
            SELECT
                symbol,
                exchange,
                toStartOfMinute(toDateTime(exch_ts / 1000000000)) AS bucket,
                argMin(price_scaled, exch_ts) AS open_scaled,
                max(price_scaled) AS high_scaled,
                min(price_scaled) AS low_scaled,
                argMax(price_scaled, exch_ts) AS close_scaled,
                sum(volume) AS volume,
                count() AS tick_count
            FROM hft.market_data
            WHERE type = 'Tick' AND price_scaled > 0
            GROUP BY symbol, exchange, bucket
            """,
            """
            CREATE VIEW IF NOT EXISTS hft.candles_1m_mv AS
            SELECT
                symbol,
                exchange,
                bucket AS window,
                open_scaled,
                high_scaled,
                low_scaled,
                close_scaled,
                volume,
                tick_count
            FROM hft.ohlcv_1m
            """,
        ],
    )
    return True
