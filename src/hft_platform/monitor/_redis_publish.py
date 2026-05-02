"""Best-effort Redis publisher for live monitor snapshots.

Thread safety (P0-D1, 2026-04-24)
--------------------------------
``publish_market_data`` is called from the engine event loop, which reuses
per-symbol payload dicts (including in-place mutated ``bids_price`` /
``bids_vol`` / ``asks_price`` / ``asks_vol`` lists) to avoid per-tick
allocations. Enqueuing the live dict reference would let the background
worker thread read/serialize the lists **while** the engine loop is
overwriting them element-by-element — a torn read visible to the Redis
monitor (and a data-integrity violation).

Fix: **serialize-at-enqueue**. ``publish_market_data`` now snapshots the
payload to immutable bytes on the caller's thread (engine loop) before
putting it on ``self._queue``. The worker thread only handles opaque
bytes, so no torn reads are possible regardless of downstream mutation.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

from structlog import get_logger

try:
    import orjson

    def _dumps(obj: object) -> str:
        return orjson.dumps(obj).decode()

    def _dumps_bytes(obj: object) -> bytes:
        return orjson.dumps(obj)

except ImportError:
    import json

    _dumps = json.dumps

    def _dumps_bytes(obj: object) -> bytes:
        return json.dumps(obj).encode("utf-8")


from hft_platform.core import timebase
from hft_platform.monitor._redis_wire import _DEFAULT_TIMEOUT_S, RedisClient

logger = get_logger("monitor.redis_publish")

# Heartbeat interval / TTL
_HEARTBEAT_INTERVAL_S = 5.0
_HEARTBEAT_TTL_S = 15


class MonitorLivePublisher:
    """Non-blocking monitor snapshot publisher using a background worker thread."""

    __slots__ = (
        "_client",
        "_key_prefix",
        "_ring_size",
        "_queue",
        "_thread",
        "_running",
        "_dropped",
        "_last_heartbeat",
        "_published",
        "_latest_keys",
        "_ring_keys",
        "_body_template",
        "_serialize_errors",
    )

    def __init__(
        self,
        host: str,
        port: int,
        password: str = "",
        key_prefix: str = "monitor:l1",
        ring_size: int = 256,
        queue_size: int = 2048,
    ) -> None:
        self._client = RedisClient(host=host, port=port, password=password, timeout_s=_DEFAULT_TIMEOUT_S)
        self._key_prefix = key_prefix.rstrip(":")
        self._ring_size = max(16, int(ring_size))
        # Queue carries (symbol, body_bytes) tuples — immutable once enqueued,
        # so the worker thread never reads producer-mutable state (P0-D1).
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=max(64, int(queue_size)))
        self._thread: threading.Thread | None = None
        self._running = False
        self._dropped = 0
        self._last_heartbeat = 0.0
        self._published = 0
        self._serialize_errors = 0
        # Pre-computed key caches (populated lazily per symbol)
        self._latest_keys: dict[str, str] = {}
        self._ring_keys: dict[str, str] = {}
        # Reusable body dict — safe because worker is single-threaded
        self._body_template: dict[str, Any] = {
            "symbol": "",
            "ingest_ts": 0,
            "bids_price": [],
            "asks_price": [],
            "bids_vol": [],
            "asks_vol": [],
            "price_scaled": 0,
            "volume": 0,
        }

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def published(self) -> int:
        return self._published

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="monitor-live-publisher", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._client.close()

    def publish_market_data(self, payload: dict[str, Any]) -> None:
        """Serialize *payload* on the caller's thread and enqueue as bytes.

        Called from the engine event loop. ``payload`` is usually a cached
        per-symbol dict whose ``bids_price`` / ``asks_price`` / ``bids_vol`` /
        ``asks_vol`` lists are mutated in-place on every tick. Snapshotting to
        bytes here (before ``put_nowait``) guarantees the worker thread never
        observes torn mid-mutation state (P0-D1, 2026-04-24).
        """
        if not self._running:
            return
        if not payload.get("bids_price") or not payload.get("asks_price"):
            return
        symbol = str(payload.get("symbol", ""))
        if not symbol:
            return
        try:
            # Build a fresh normalized body and serialize while still holding
            # the GIL on the producer thread. ``list(...)`` creates a defensive
            # copy so orjson's internal iteration cannot observe concurrent
            # mutation even if the underlying list is reused elsewhere.
            body = {
                "symbol": symbol,
                "ingest_ts": int(payload.get("ingest_ts", 0) or 0),
                "bids_price": list(payload.get("bids_price") or ()),
                "asks_price": list(payload.get("asks_price") or ()),
                "bids_vol": list(payload.get("bids_vol") or ()),
                "asks_vol": list(payload.get("asks_vol") or ()),
                "price_scaled": int(payload.get("price_scaled", 0) or 0),
                "volume": int(payload.get("volume", 0) or 0),
            }
            body_bytes = _dumps_bytes(body)
        except Exception as exc:  # noqa: BLE001 — never crash the engine loop
            self._serialize_errors += 1
            if self._serialize_errors % 100 == 1:
                logger.warning(
                    "monitor_live_serialize_error",
                    error=str(exc),
                    total_errors=self._serialize_errors,
                )
            return
        try:
            self._queue.put_nowait((symbol, body_bytes))
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("monitor_live_drop", dropped=self._dropped)

    def _run(self) -> None:
        while self._running:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                self._maybe_heartbeat()
                continue
            try:
                symbol, body_bytes = item
                self._publish_bytes(symbol, body_bytes)
                self._published += 1
                self._maybe_heartbeat()
            except Exception as exc:
                logger.warning("monitor_live_publish_failed", error=str(exc))
                self._client.close()

    def _maybe_heartbeat(self) -> None:
        """Write heartbeat key if enough time has elapsed."""
        now = time.monotonic()
        if now - self._last_heartbeat < _HEARTBEAT_INTERVAL_S:
            return
        try:
            hb_key = f"{self._key_prefix}:heartbeat"
            hb_body = _dumps(
                {
                    "ts_ns": timebase.now_ns(),
                    "pid": os.getpid(),
                },
            )
            self._client.request("SET", hb_key, hb_body, "EX", str(_HEARTBEAT_TTL_S))
            self._last_heartbeat = now
        except Exception as _exc:  # noqa: BLE001
            logger.debug("monitor_heartbeat_failed")

    def _publish_bytes(self, symbol: str, body_bytes: bytes) -> None:
        """Publish pre-serialized body bytes to Redis.

        The worker thread owns this path; ``body_bytes`` is immutable, so there
        is no cross-thread torn-read hazard. Redis RESP accepts str or bytes
        values — we decode once per publish because ``RedisClient.pipeline``
        expects str tokens.
        """
        body = body_bytes.decode("utf-8")
        latest_key = self._latest_keys.get(symbol)
        if latest_key is None:
            latest_key = f"{self._key_prefix}:latest:{symbol}"
            self._latest_keys[symbol] = latest_key
        ring_key = self._ring_keys.get(symbol)
        if ring_key is None:
            ring_key = f"{self._key_prefix}:ring:{symbol}"
            self._ring_keys[symbol] = ring_key
        self._client.pipeline(
            ("SET", latest_key, body, "EX", "300"),
            ("LPUSH", ring_key, body),
            ("LTRIM", ring_key, "0", str(self._ring_size - 1)),
        )

    def _publish_now(self, payload: dict[str, Any]) -> None:
        """Legacy dict-in publish path.

        Retained for backward-compat in tests. Production hot path now goes
        through ``publish_market_data`` → worker → ``_publish_bytes``, which
        serializes on the producer thread to avoid torn reads (P0-D1).
        """
        symbol = str(payload["symbol"])
        body_bytes = _dumps_bytes(
            {
                "symbol": symbol,
                "ingest_ts": int(payload["ingest_ts"]),
                "bids_price": list(payload.get("bids_price") or ()),
                "asks_price": list(payload.get("asks_price") or ()),
                "bids_vol": list(payload.get("bids_vol") or ()),
                "asks_vol": list(payload.get("asks_vol") or ()),
                "price_scaled": int(payload.get("price_scaled", 0) or 0),
                "volume": int(payload.get("volume", 0) or 0),
            }
        )
        self._publish_bytes(symbol, body_bytes)
