"""Best-effort Redis publisher for live monitor snapshots."""

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

except ImportError:
    import json

    _dumps = json.dumps  # type: ignore[assignment]

from hft_platform.core import timebase
from hft_platform.monitor._redis_wire import RedisClient

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
        self._client = RedisClient(host=host, port=port, password=password)
        self._key_prefix = key_prefix.rstrip(":")
        self._ring_size = max(16, int(ring_size))
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max(64, int(queue_size)))
        self._thread: threading.Thread | None = None
        self._running = False
        self._dropped = 0
        self._last_heartbeat = 0.0
        self._published = 0
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
        if not self._running:
            return
        if not payload.get("bids_price") or not payload.get("asks_price"):
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("monitor_live_drop", dropped=self._dropped)

    def _run(self) -> None:
        while self._running:
            try:
                payload = self._queue.get(timeout=0.25)
            except queue.Empty:
                self._maybe_heartbeat()
                continue
            try:
                self._publish_now(payload)
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
        except Exception:
            logger.debug("monitor_heartbeat_failed")

    def _publish_now(self, payload: dict[str, Any]) -> None:
        symbol = str(payload["symbol"])
        # Mutate reusable body template in-place (single-threaded worker)
        t = self._body_template
        t["symbol"] = symbol
        t["ingest_ts"] = int(payload["ingest_ts"])
        t["bids_price"] = payload.get("bids_price", [])
        t["asks_price"] = payload.get("asks_price", [])
        t["bids_vol"] = payload.get("bids_vol", [])
        t["asks_vol"] = payload.get("asks_vol", [])
        t["price_scaled"] = int(payload.get("price_scaled", 0) or 0)
        t["volume"] = int(payload.get("volume", 0) or 0)
        body = _dumps(t)
        # Use cached keys
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
