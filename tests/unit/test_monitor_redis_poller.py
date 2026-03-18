from __future__ import annotations

import json

import pytest

from hft_platform.monitor._redis_poller import RedisPoller


class _FakeRedisClient:
    def __init__(self, responses: dict[tuple[str, ...], object] | None = None) -> None:
        self.responses: dict[tuple[str, ...], object] = responses or {}
        self._connected = False
        self.host = "127.0.0.1"
        self.port = 6379

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def request(self, *parts: str):
        key = tuple(parts)
        if key not in self.responses:
            raise AssertionError(f"unexpected redis command: {parts!r}")
        return self.responses[key]


def _row(symbol: str, ingest_ts: int) -> str:
    return json.dumps(
        {
            "symbol": symbol,
            "ingest_ts": ingest_ts,
            "bids_price": [210000000],
            "asks_price": [210500000],
            "bids_vol": [10],
            "asks_vol": [8],
            "price_scaled": 0,
            "volume": 1,
        }
    )


def test_redis_poller_fetches_recent_and_new_rows() -> None:
    poller = RedisPoller(
        host="127.0.0.1",
        port=6379,
        symbols=("TMFC6", "2330"),
        key_prefix="monitor:l1",
        ring_size=8,
        batch_limit=4,
    )
    poller._client = _FakeRedisClient(
        {
            ("LRANGE", "monitor:l1:ring:TMFC6", "0", "1"): [_row("TMFC6", 2), _row("TMFC6", 1)],
            (
                "MGET",
                "monitor:l1:latest:TMFC6",
                "monitor:l1:latest:2330",
            ): [_row("TMFC6", 3), None],
            ("LRANGE", "monitor:l1:ring:TMFC6", "0", "3"): [_row("TMFC6", 3), _row("TMFC6", 2), _row("TMFC6", 1)],
        }
    )

    recent = poller.fetch_recent_valid("TMFC6", limit=2, min_ingest_ts=0)
    assert [row.ingest_ts for row in recent] == [1, 2]

    rows_by_symbol = poller.poll({"TMFC6": 1, "2330": 0})
    assert [row.ingest_ts for row in rows_by_symbol["TMFC6"]] == [2, 3]
    assert rows_by_symbol["2330"] == []


def test_poll_empty_cursors_returns_empty() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=())
    assert poller.poll({}) == {}


def test_poll_raises_connection_error_on_failure() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=("X",), max_retries=3)
    fake = _FakeRedisClient()
    fake.responses = {}  # no responses → will raise
    poller._client = fake
    with pytest.raises(ConnectionError, match="Redis poll failed"):
        poller.poll({"X": 0})
    assert poller.retry_count == 1


def test_try_reconnect_increments_retry() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=(), max_retries=3)
    fake = _FakeRedisClient()
    poller._client = fake
    # Simulate a disconnect
    poller._retry_count = 1
    poller._next_retry_at = 0.0
    # connect succeeds
    result = poller.try_reconnect()
    assert result is True
    assert poller.retry_count == 0  # reset on success


def test_try_reconnect_raises_after_max_retries() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=(), max_retries=2)
    poller._retry_count = 2
    poller._last_error = "test error"
    with pytest.raises(RuntimeError, match="2 attempts"):
        poller.try_reconnect()


def test_backoff_seconds_exponential() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=())
    poller._retry_count = 0
    assert poller.get_backoff_seconds() == 1.0
    poller._retry_count = 3
    assert poller.get_backoff_seconds() == 8.0
    poller._retry_count = 10
    assert poller.get_backoff_seconds() == 30.0  # capped


def test_decode_row_missing_field_raises() -> None:
    bad_json = json.dumps({"ingest_ts": 1})  # missing "symbol"
    with pytest.raises(KeyError):
        RedisPoller._decode_row(bad_json)
