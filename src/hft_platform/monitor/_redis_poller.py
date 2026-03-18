"""Redis-backed live cache poller for the signal monitor."""

from __future__ import annotations

import time

from structlog import get_logger

try:
    import orjson

    _loads = orjson.loads
except ImportError:
    import json

    _loads = json.loads  # type: ignore[assignment]

from hft_platform.core import timebase
from hft_platform.monitor._redis_wire import RedisClient
from hft_platform.monitor._types import RowView

logger = get_logger("monitor.redis_poller")

# Max age (seconds) for heartbeat to be considered fresh
_HEARTBEAT_MAX_AGE_S = 15.0


class RedisPoller:
    """Reads latest/recent symbol snapshots from Redis instead of ClickHouse."""

    __slots__ = (
        "_client",
        "_symbols",
        "_retry_count",
        "_max_retries",
        "_last_error",
        "_next_retry_at",
        "_key_prefix",
        "_ring_size",
        "_batch_limit",
        "_heartbeat_stale",
    )

    def __init__(
        self,
        host: str,
        port: int,
        symbols: tuple[str, ...],
        password: str = "",
        key_prefix: str = "monitor:l1",
        ring_size: int = 256,
        batch_limit: int = 200,
        max_retries: int = 20,
    ) -> None:
        self._client = RedisClient(host=host, port=port, password=password, timeout_s=0.5)
        self._symbols = symbols
        self._retry_count = 0
        self._max_retries = max_retries
        self._last_error = ""
        self._next_retry_at = 0.0
        self._key_prefix = key_prefix.rstrip(":")
        self._ring_size = max(1, int(ring_size))
        self._batch_limit = max(1, int(batch_limit))
        self._heartbeat_stale = False

    def connect(self) -> None:
        self._client.connect()
        self._retry_count = 0
        self._next_retry_at = 0.0
        logger.info("redis_connected", host=self._client.host, port=self._client.port)

    def close(self) -> None:
        self._client.close()

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def heartbeat_stale(self) -> bool:
        return self._heartbeat_stale

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        if not cursors:
            return {}
        try:
            self._check_heartbeat()
            latest_keys = [self._latest_key(symbol) for symbol in cursors]
            latest_values = self._client.request("MGET", *latest_keys)
            self._retry_count = 0
            rows_by_symbol: dict[str, list[RowView]] = {symbol: [] for symbol in cursors}
            for symbol, latest_json in zip(cursors, latest_values, strict=False):
                if not latest_json:
                    continue
                latest = self._decode_row(latest_json)
                if latest.ingest_ts <= cursors[symbol]:
                    continue
                ring_values = self._client.request(
                    "LRANGE",
                    self._ring_key(symbol),
                    "0",
                    str(min(self._batch_limit, self._ring_size) - 1),
                )
                rows = [
                    self._decode_row(raw)
                    for raw in (ring_values or [])
                    if raw is not None
                ]
                rows.reverse()
                rows_by_symbol[symbol] = [row for row in rows if row.ingest_ts > cursors[symbol]]
            return rows_by_symbol
        except Exception as exc:
            self._register_disconnect(str(exc))
            raise ConnectionError(f"Redis poll failed: {exc}") from exc

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        try:
            ring_values = self._client.request(
                "LRANGE",
                self._ring_key(symbol),
                "0",
                str(min(max(1, int(limit)), self._ring_size) - 1),
            )
            self._retry_count = 0
            rows = [
                self._decode_row(raw)
                for raw in (ring_values or [])
                if raw is not None
            ]
            rows.reverse()
            return [row for row in rows if row.ingest_ts >= min_ingest_ts]
        except Exception as exc:
            self._register_disconnect(str(exc))
            raise ConnectionError(f"Redis replay failed: {exc}") from exc

    def try_reconnect(self) -> bool:
        if self._retry_count >= self._max_retries:
            raise RuntimeError(
                f"Redis reconnection failed after {self._max_retries} attempts: {self._last_error}"
            )
        if self.remaining_backoff_seconds() > 0:
            return False
        try:
            self.connect()
            return True
        except Exception as exc:
            self._register_disconnect(str(exc))
            return False

    def get_backoff_seconds(self) -> float:
        return min(2 ** self._retry_count, 30)

    def remaining_backoff_seconds(self) -> float:
        return max(0.0, self._next_retry_at - time.monotonic())

    def _register_disconnect(self, error: str) -> None:
        self._retry_count += 1
        self._last_error = error
        self._client.close()
        self._next_retry_at = time.monotonic() + self.get_backoff_seconds()

    def _check_heartbeat(self) -> None:
        """Check publisher heartbeat key. Sets _heartbeat_stale flag."""
        try:
            hb_key = f"{self._key_prefix}:heartbeat"
            raw = self._client.request("GET", hb_key)
            if raw is None:
                self._heartbeat_stale = True
                return
            hb = _loads(raw)
            age_s = (timebase.now_ns() - int(hb["ts_ns"])) / 1e9
            self._heartbeat_stale = age_s > _HEARTBEAT_MAX_AGE_S
        except Exception:
            self._heartbeat_stale = True

    def _latest_key(self, symbol: str) -> str:
        return f"{self._key_prefix}:latest:{symbol}"

    def _ring_key(self, symbol: str) -> str:
        return f"{self._key_prefix}:ring:{symbol}"

    @staticmethod
    def _decode_row(raw: str) -> RowView:
        payload = _loads(raw)
        return RowView(
            symbol=str(payload["symbol"]),
            ingest_ts=int(payload["ingest_ts"]),
            bids_price=payload.get("bids_price", []),
            asks_price=payload.get("asks_price", []),
            bids_vol=payload.get("bids_vol", []),
            asks_vol=payload.get("asks_vol", []),
            price_scaled=int(payload.get("price_scaled", 0) or 0),
            volume=int(payload.get("volume", 0) or 0),
        )
