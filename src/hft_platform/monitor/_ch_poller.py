"""Incremental ClickHouse poller with batched multi-symbol queries."""

from __future__ import annotations

import time
from typing import Any

from structlog import get_logger

from hft_platform.monitor._types import RowView

logger = get_logger("monitor.ch_poller")

# Batched SQL: single query for all symbols, partitioned client-side
_POLL_BATCH_SQL = """\
SELECT symbol, ingest_ts, bids_price, asks_price, bids_vol, asks_vol, price_scaled, volume
FROM hft.market_data
WHERE symbol IN ({{symbols:Array(String)}})
  AND ingest_ts > {{min_cursor:Int64}}
ORDER BY symbol, ingest_ts
LIMIT {limit}
SETTINGS max_memory_usage=500000000
"""

_REPLAY_TS_SQL = """\
SELECT ingest_ts
FROM hft.market_data
WHERE symbol = {{symbol:String}}
  AND ingest_ts >= {{min_ingest_ts:Int64}}
  AND length(bids_price) > 0
  AND length(asks_price) > 0
  AND length(bids_vol) > 0
  AND length(asks_vol) > 0
  AND length(bids_price) = length(bids_vol)
  AND length(asks_price) = length(asks_vol)
ORDER BY ingest_ts DESC
LIMIT {limit}
SETTINGS max_memory_usage=500000000
"""

_REPLAY_ROWS_SQL = """\
SELECT symbol, ingest_ts, bids_price, asks_price, bids_vol, asks_vol, price_scaled, volume
FROM hft.market_data
WHERE symbol = {symbol:String}
  AND ingest_ts IN ({ts_list:Array(Int64)})
ORDER BY ingest_ts
SETTINGS max_memory_usage=500000000
"""


class CHPoller:
    """Manages ClickHouse connection and incremental tick polling."""

    __slots__ = (
        "_host",
        "_port",
        "_client",
        "_symbols",
        "_user",
        "_password",
        "_batch_limit",
        "_retry_count",
        "_max_retries",
        "_last_error",
        "_next_retry_at",
        "_rows_by_symbol",
    )

    def __init__(
        self,
        host: str,
        port: int,
        symbols: tuple[str, ...],
        user: str = "default",
        password: str = "",
        batch_limit: int = 200,
        max_retries: int = 20,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._batch_limit = batch_limit
        self._client: Any = None
        self._symbols = symbols
        self._max_retries = max_retries
        self._retry_count = 0
        self._last_error: str = ""
        self._next_retry_at = 0.0
        self._rows_by_symbol: dict[str, list[Any]] = {s: [] for s in symbols}

    def connect(self) -> None:
        """Establish CH connection."""
        import clickhouse_connect

        self._client = clickhouse_connect.get_client(
            host=self._host,
            port=self._port,
            username=self._user,
            password=self._password,
        )
        self._retry_count = 0
        self._next_retry_at = 0.0
        logger.info("ch_connected", host=self._host, port=self._port)

    def close(self) -> None:
        """Close CH connection."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                pass
            self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def last_error(self) -> str:
        return self._last_error

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        """Fetch new ticks for all symbols in a single batched query."""
        if self._client is None:
            raise ConnectionError("Not connected to ClickHouse")

        if not cursors:
            return {}

        try:
            self._retry_count = 0
            symbols_list = list(cursors.keys())
            min_cursor = min(cursors.values())
            n_symbols = len(symbols_list)
            total_limit = self._batch_limit * n_symbols

            sql = _POLL_BATCH_SQL.format(limit=total_limit)
            result = self._client.query(
                sql,
                parameters={
                    "symbols": symbols_list,
                    "min_cursor": int(min_cursor),
                },
            )

            # Clear previous results and reuse pre-allocated dict/lists
            for lst in self._rows_by_symbol.values():
                lst.clear()

            # Partition results by symbol with per-symbol cursor filtering
            for raw_row in result.result_rows:
                rv = _to_row_view(raw_row)
                sym = rv.symbol
                if sym in cursors and rv.ingest_ts > cursors[sym]:
                    sym_list = self._rows_by_symbol.get(sym)
                    if sym_list is not None:
                        sym_list.append(rv)

            return self._rows_by_symbol
        except Exception as exc:
            self._register_disconnect(str(exc))
            raise ConnectionError(f"CH query failed: {exc}") from exc

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        """Replay recent valid rows for a symbol, ordered oldest -> newest."""
        if self._client is None:
            raise ConnectionError("Not connected to ClickHouse")

        replay_limit = max(1, int(limit))
        last_exc: Exception | None = None
        while replay_limit >= 8:
            try:
                ts_rows = self._client.query(
                    _REPLAY_TS_SQL.format(limit=replay_limit),
                    parameters={
                        "symbol": symbol,
                        "min_ingest_ts": int(min_ingest_ts),
                    },
                )
                ts_list = [int(row[0]) for row in ts_rows.result_rows]
                if not ts_list:
                    return []

                row_result = self._client.query(
                    _REPLAY_ROWS_SQL,
                    parameters={
                        "symbol": symbol,
                        "ts_list": ts_list,
                    },
                )
                self._retry_count = 0
                return [_to_row_view(row) for row in row_result.result_rows]
            except Exception as exc:
                last_exc = exc
                if "MEMORY_LIMIT_EXCEEDED" not in str(exc) or replay_limit <= 8:
                    break
                next_limit = max(8, replay_limit // 2)
                logger.warning(
                    "replay_limit_reduced",
                    symbol=symbol,
                    previous_limit=replay_limit,
                    next_limit=next_limit,
                    error=str(exc),
                )
                replay_limit = next_limit

        assert last_exc is not None  # noqa: S101
        self._register_disconnect(str(last_exc))
        raise ConnectionError(f"CH replay failed: {last_exc}") from last_exc

    def try_reconnect(self) -> bool:
        """Attempt reconnection with exponential backoff timing.

        Returns True if connected, False if should retry later.
        Raises RuntimeError if max retries exceeded.
        """
        if self._retry_count >= self._max_retries:
            raise RuntimeError(f"CH reconnection failed after {self._max_retries} attempts: {self._last_error}")

        if self.remaining_backoff_seconds() > 0:
            return False

        backoff_s = self.get_backoff_seconds()
        logger.warning(
            "ch_reconnecting",
            attempt=self._retry_count,
            backoff_s=backoff_s,
        )

        try:
            self.connect()
            return True
        except Exception as exc:
            self._register_disconnect(str(exc))
            return False

    def get_backoff_seconds(self) -> float:
        """Get current backoff delay in seconds."""
        return min(2**self._retry_count, 30)

    def remaining_backoff_seconds(self) -> float:
        """Return remaining wait time before the next reconnect attempt."""
        return max(0.0, self._next_retry_at - time.monotonic())

    def _register_disconnect(self, error: str) -> None:
        """Record a disconnect and schedule the next retry window."""
        self._retry_count += 1
        self._last_error = error
        self._client = None
        self._next_retry_at = time.monotonic() + self.get_backoff_seconds()


def _to_row_view(row: Any) -> RowView:
    return RowView(
        symbol=row[0],
        ingest_ts=int(row[1]),
        bids_price=row[2],
        asks_price=row[3],
        bids_vol=row[4],
        asks_vol=row[5],
        price_scaled=row[6],
        volume=row[7],
    )
