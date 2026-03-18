from __future__ import annotations

import json
import time

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

    def pipeline(self, *commands: tuple[str, ...]) -> list[object]:
        results: list[object] = []
        for cmd in commands:
            key = tuple(cmd)
            if key not in self.responses:
                raise AssertionError(f"unexpected pipeline command: {cmd!r}")
            results.append(self.responses[key])
        return results


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


def _hb(ts_ns: int) -> str:
    return json.dumps({"ts_ns": ts_ns})


def test_redis_poller_fetches_recent_and_new_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hft_platform.core.timebase.now_ns", lambda: 100_000_000_000)
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
            # MGET now includes heartbeat key as first element
            (
                "MGET",
                "monitor:l1:heartbeat",
                "monitor:l1:latest:TMFC6",
                "monitor:l1:latest:2330",
            ): [_hb(100_000_000_000), _row("TMFC6", 3), None],
            # LRANGE via pipeline
            ("LRANGE", "monitor:l1:ring:TMFC6", "0", "3"): [
                _row("TMFC6", 3),
                _row("TMFC6", 2),
                _row("TMFC6", 1),
            ],
        }
    )

    recent = poller.fetch_recent_valid("TMFC6", limit=2, min_ingest_ts=0)
    assert [row.ingest_ts for row in recent] == [1, 2]

    rows_by_symbol = poller.poll({"TMFC6": 1, "2330": 0})
    assert [row.ingest_ts for row in rows_by_symbol["TMFC6"]] == [2, 3]
    assert rows_by_symbol["2330"] == []
    assert poller.heartbeat_stale is False


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


# ---------- Unit 7: heartbeat, malformed JSON, reconnect cycle ---------- #


def test_heartbeat_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hft_platform.core.timebase.now_ns", lambda: 100_000_000_000)
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=("X",))
    poller._parse_heartbeat(_hb(100_000_000_000 - 5_000_000_000))  # 5s old
    assert poller.heartbeat_stale is False


def test_heartbeat_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hft_platform.core.timebase.now_ns", lambda: 100_000_000_000)
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=("X",))
    poller._parse_heartbeat(_hb(100_000_000_000 - 20_000_000_000))  # 20s old
    assert poller.heartbeat_stale is True


def test_heartbeat_missing() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=("X",))
    poller._parse_heartbeat(None)
    assert poller.heartbeat_stale is True


def test_heartbeat_malformed() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=("X",))
    poller._parse_heartbeat("not-json{{{")
    assert poller.heartbeat_stale is True


def test_poll_malformed_json_in_mget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed latest JSON should raise ConnectionError (wrapped)."""
    monkeypatch.setattr("hft_platform.core.timebase.now_ns", lambda: 100_000_000_000)
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=("X",))
    poller._client = _FakeRedisClient(
        {
            ("MGET", "monitor:l1:heartbeat", "monitor:l1:latest:X"): [
                _hb(100_000_000_000),
                "not-valid-json{{{",
            ],
        }
    )
    with pytest.raises(ConnectionError, match="Redis poll failed"):
        poller.poll({"X": 0})


def test_reconnect_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full cycle: disconnect → backoff → reconnect → success."""
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=(), max_retries=5)
    fake = _FakeRedisClient()
    poller._client = fake

    # Simulate disconnect
    poller._register_disconnect("test disconnect")
    assert poller.retry_count == 1
    assert fake.connected is False
    assert poller.remaining_backoff_seconds() > 0

    # Skip backoff by resetting next_retry_at
    poller._next_retry_at = 0.0

    # Reconnect succeeds
    result = poller.try_reconnect()
    assert result is True
    assert poller.retry_count == 0
    assert fake.connected is True


def test_remaining_backoff_seconds() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=())
    poller._next_retry_at = time.monotonic() + 5.0
    assert 4.0 < poller.remaining_backoff_seconds() <= 5.0

    poller._next_retry_at = 0.0
    assert poller.remaining_backoff_seconds() == 0.0


def test_fetch_recent_valid_empty_ring() -> None:
    poller = RedisPoller(host="127.0.0.1", port=6379, symbols=("X",))
    poller._client = _FakeRedisClient(
        {
            ("LRANGE", "monitor:l1:ring:X", "0", "9"): [],
        }
    )
    result = poller.fetch_recent_valid("X", limit=10, min_ingest_ts=0)
    assert result == []


def test_key_caching() -> None:
    """Pre-computed keys should be used and new symbols should be cached lazily."""
    poller = RedisPoller(
        host="127.0.0.1",
        port=6379,
        symbols=("A", "B"),
        key_prefix="m:l1",
    )
    # Pre-computed for known symbols
    assert poller._latest_key("A") == "m:l1:latest:A"
    assert poller._ring_key("B") == "m:l1:ring:B"
    # Lazy cache for unknown symbol
    assert poller._latest_key("C") == "m:l1:latest:C"
    assert "C" in poller._latest_keys_cache
