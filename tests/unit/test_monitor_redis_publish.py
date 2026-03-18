"""Tests for MonitorLivePublisher (Phase A3 + B1)."""

from __future__ import annotations

import io
import time

from hft_platform.monitor._redis_publish import MonitorLivePublisher


class _RespStream:
    """Captures writes and returns +OK RESP responses for each command."""

    def __init__(self) -> None:
        self.written = bytearray()
        self._read_buf = io.BytesIO()

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        # Count *-prefixed commands and queue +OK for each
        n_commands = data.count(b"*")
        pos = self._read_buf.tell()
        self._read_buf.seek(0, 2)  # seek to end
        for _ in range(n_commands):
            self._read_buf.write(b"+OK\r\n")
        self._read_buf.seek(pos)  # restore read position
        return len(data)

    def read(self, n: int = -1) -> bytes:
        return self._read_buf.read(n)

    def readline(self) -> bytes:
        return self._read_buf.readline()


class _FakeRedisClient:
    """Stub RedisClient with _RespStream for response simulation."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self.port = 6379
        self._sock = True  # pretend connected
        self._stream = _RespStream()

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self._sock = None

    def request(self, *parts: str) -> str:
        # Record command for assertion
        self._stream.written.extend("|".join(parts).encode() + b"\n")
        return "OK"

    def pipeline(self, *commands: tuple[str, ...]) -> list[str]:
        for cmd in commands:
            self._stream.written.extend("|".join(cmd).encode() + b"\n")
        return ["OK"] * len(commands)


def _make_payload(symbol: str = "2330", ingest_ts: int = 1000) -> dict:
    return {
        "symbol": symbol,
        "ingest_ts": ingest_ts,
        "bids_price": [210_000_000],
        "asks_price": [210_500_000],
        "bids_vol": [10],
        "asks_vol": [8],
        "price_scaled": 0,
        "volume": 1,
    }


def test_publish_noop_when_not_running() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379)
    # Not started — publish should be no-op
    pub.publish_market_data(_make_payload())
    assert pub._queue.empty()


def test_publish_drops_on_queue_full() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379, queue_size=64)
    pub._running = True  # simulate started without thread
    # Fill queue
    for i in range(64):
        pub.publish_market_data(_make_payload(ingest_ts=i))
    assert pub._queue.full()
    # Next publish should drop
    pub.publish_market_data(_make_payload(ingest_ts=999))
    assert pub.dropped == 1


def test_publish_skips_empty_book() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379)
    pub._running = True
    # No bids_price → should skip
    pub.publish_market_data({"symbol": "2330", "ingest_ts": 1, "bids_price": [], "asks_price": [1]})
    assert pub._queue.empty()
    # No asks_price → should skip
    pub.publish_market_data({"symbol": "2330", "ingest_ts": 1, "bids_price": [1], "asks_price": []})
    assert pub._queue.empty()


def test_publish_now_generates_correct_redis_commands() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379, key_prefix="monitor:l1", ring_size=32)
    fake = _FakeRedisClient()
    pub._client = fake

    payload = _make_payload(symbol="TMFC6", ingest_ts=12345)
    pub._publish_now(payload)

    written = fake._stream.written.decode("utf-8", errors="replace")
    # Verify SET with EX for latest key
    assert "monitor:l1:latest:TMFC6" in written
    assert "EX" in written
    assert "300" in written
    # Verify LPUSH for ring key
    assert "monitor:l1:ring:TMFC6" in written
    assert "LPUSH" in written
    # Verify LTRIM
    assert "LTRIM" in written
    assert "31" in written  # ring_size - 1


def test_start_close_lifecycle() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379)
    pub._client = _FakeRedisClient()
    pub.start()
    assert pub._running is True
    assert pub._thread is not None
    assert pub._thread.is_alive()
    # Double start should be no-op
    pub.start()
    pub.close()
    assert pub._running is False
    assert pub._thread is None


def test_heartbeat_writes_key() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379, key_prefix="test:l1")
    fake = _FakeRedisClient()
    pub._client = fake
    pub._last_heartbeat = 0.0  # force heartbeat to fire
    pub._maybe_heartbeat()
    assert pub._last_heartbeat > 0
    written = fake._stream.written.decode("utf-8", errors="replace")
    assert "test:l1:heartbeat" in written
    assert "EX" in written
    assert "15" in written


def test_heartbeat_not_written_when_recent() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379)
    fake = _FakeRedisClient()
    pub._client = fake
    pub._last_heartbeat = time.monotonic()  # just written
    pub._maybe_heartbeat()
    # Stream should have no writes
    assert len(fake._stream.written) == 0


def test_published_counter_not_incremented_by_publish_now() -> None:
    """_published is only incremented in _run loop, not directly."""
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379)
    fake = _FakeRedisClient()
    pub._client = fake
    assert pub.published == 0
    pub._publish_now(_make_payload())
    assert pub.published == 0  # only incremented in _run()
