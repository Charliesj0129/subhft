"""Tests for MonitorLivePublisher (Phase A3 + B1)."""

from __future__ import annotations

import io
import queue
import time
from typing import Any

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


def test_publish_snapshots_payload_at_enqueue_not_dequeue() -> None:
    """P0-D1 regression: enqueued payload must be immutable to the consumer.

    Simulates the engine-loop producer pattern: the producer keeps one
    cached dict whose ``bids_price`` / ``asks_price`` lists are mutated in
    place on each tick. Before the fix, the queue held a live reference and
    the consumer saw the latest mutation regardless of when the tick was
    enqueued. After the fix, the payload is serialized to bytes on enqueue,
    so the queue entry reflects the tick as observed at ``publish_market_data``
    call time.
    """
    import orjson

    pub = MonitorLivePublisher(host="127.0.0.1", port=6379, queue_size=64)
    pub._running = True

    cached = {
        "symbol": "2330",
        "ingest_ts": 1000,
        "bids_price": [100, 99, 98, 97, 96],
        "asks_price": [101, 102, 103, 104, 105],
        "bids_vol": [10, 20, 30, 40, 50],
        "asks_vol": [11, 21, 31, 41, 51],
        "price_scaled": 100,
        "volume": 1,
    }
    pub.publish_market_data(cached)

    # Simulate the next engine-loop tick mutating the cached dict in place.
    cached["ingest_ts"] = 2000
    for i in range(5):
        cached["bids_price"][i] = 900 + i  # tick 2: wildly different prices
        cached["asks_price"][i] = 1000 + i
        cached["bids_vol"][i] = 1
        cached["asks_vol"][i] = 2
    cached["price_scaled"] = 999
    cached["volume"] = 42

    # What the consumer finds in the queue should be tick 1, NOT tick 2.
    sym, body_bytes = pub._queue.get_nowait()
    assert sym == "2330"
    body = orjson.loads(body_bytes)
    assert body["ingest_ts"] == 1000, "ingest_ts must reflect enqueue-time tick"
    assert body["bids_price"] == [100, 99, 98, 97, 96], "bids_price torn read regression"
    assert body["asks_price"] == [101, 102, 103, 104, 105], "asks_price torn read regression"
    assert body["bids_vol"] == [10, 20, 30, 40, 50]
    assert body["asks_vol"] == [11, 21, 31, 41, 51]
    assert body["price_scaled"] == 100
    assert body["volume"] == 1


def test_publish_queue_carries_bytes_not_dict() -> None:
    """Queue entries must be (symbol, bytes) tuples so consumer never sees dicts."""
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379, queue_size=16)
    pub._running = True
    pub.publish_market_data(_make_payload())
    item = pub._queue.get_nowait()
    assert isinstance(item, tuple)
    assert len(item) == 2
    symbol, body = item
    assert isinstance(symbol, str)
    assert isinstance(body, (bytes, bytearray))


def test_publish_drops_without_symbol() -> None:
    pub = MonitorLivePublisher(host="127.0.0.1", port=6379, queue_size=16)
    pub._running = True
    pub.publish_market_data(
        {
            "symbol": "",
            "ingest_ts": 1,
            "bids_price": [1],
            "asks_price": [2],
        }
    )
    assert pub._queue.empty()


def test_concurrent_producer_and_consumer_no_torn_read() -> None:
    """Stress test: producer mutates cached dict while worker thread drains.

    Before the P0-D1 fix this test would sporadically observe mid-mutation
    payloads (e.g., bids_price[0] from tick N, bids_price[1] from tick N+1).
    With serialize-at-enqueue the consumer only ever sees consistent bytes.
    """
    import threading as _threading

    import orjson

    pub = MonitorLivePublisher(host="127.0.0.1", port=6379, queue_size=2048)
    pub._running = True

    stop_flag = _threading.Event()
    errors: list[str] = []

    cached: dict[str, Any] = {
        "symbol": "TXFD6",
        "ingest_ts": 0,
        "bids_price": [0] * 5,
        "asks_price": [0] * 5,
        "bids_vol": [0] * 5,
        "asks_vol": [0] * 5,
        "price_scaled": 0,
        "volume": 0,
    }

    def producer() -> None:
        tick = 0
        while not stop_flag.is_set() and tick < 2_000:
            tick += 1
            cached["ingest_ts"] = tick
            for i in range(5):
                cached["bids_price"][i] = tick * 10 + i
                cached["asks_price"][i] = tick * 10 + i + 5
                cached["bids_vol"][i] = tick + i
                cached["asks_vol"][i] = tick + i + 5
            cached["price_scaled"] = tick * 10
            cached["volume"] = tick
            pub.publish_market_data(cached)

    def consumer() -> None:
        drained = 0
        while drained < 1_000:
            try:
                sym, body_bytes = pub._queue.get(timeout=1.0)
            except queue.Empty:
                return
            body = orjson.loads(body_bytes)
            t = body["ingest_ts"]
            # All five bid levels must belong to the same tick.
            expected_bids = [t * 10 + i for i in range(5)]
            expected_asks = [t * 10 + i + 5 for i in range(5)]
            if body["bids_price"] != expected_bids or body["asks_price"] != expected_asks:
                errors.append(
                    f"torn read at tick={t}: bids={body['bids_price']} asks={body['asks_price']}"
                )
                return
            drained += 1

    t_prod = _threading.Thread(target=producer, daemon=True)
    t_cons = _threading.Thread(target=consumer, daemon=True)
    t_cons.start()
    t_prod.start()
    t_cons.join(timeout=5.0)
    stop_flag.set()
    t_prod.join(timeout=5.0)

    assert not errors, f"found {len(errors)} torn reads; first: {errors[0]}"
