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

Optional per-symbol minimum publish interval
--------------------------------------------
``min_interval_ms`` (service env ``HFT_MONITOR_REDIS_MIN_INTERVAL_MS``,
default 0) drops a tick when the previous tick **enqueued** for the same
symbol is less than that many milliseconds old, measured with
``time.monotonic_ns`` (injectable through ``clock_ns`` for tests). The
check runs first in ``publish_market_data``, right after the existing
empty-book / no-symbol guards and before any list copy or serialization, so a
skipped tick costs one clock read and one dict lookup inside this method and
reaches neither the queue nor the worker thread. The caller
(``MarketDataService._publish_to_redis``) still refreshes its cached payload on
every event, so the saving is the copy, serialization and enqueue only. Skipped
ticks are counted in ``skipped``. A tick lost to a full queue opens no interval window: the drop
path is unchanged and ``dropped`` still counts it.

With 0 (the default) the clock is never read and every path behaves exactly as
before. Content of an accepted tick is identical either way. What changes is
volume: the monitor's per-symbol ring (``LPUSH`` + ``LTRIM`` to ``ring_size``)
then holds fewer entries, at most one per interval, so a ring of
``ring_size`` entries spans ``ring_size * min_interval_ms`` instead of
``ring_size`` ticks. The ``latest`` key is likewise only as fresh as the last
accepted tick: a tick skipped at the end of a burst is not published later.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.monitor._redis_wire import _DEFAULT_TIMEOUT_S, RedisClient
from hft_platform.utils.jsonfast import dumps as _dumps_bytes
from hft_platform.utils.jsonfast import dumps_str as _dumps

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
        "_skipped",
        "_min_interval_ns",
        "_clock_ns",
        "_last_accept_ns",
    )

    def __init__(
        self,
        host: str,
        port: int,
        password: str = "",
        key_prefix: str = "monitor:l1",
        ring_size: int = 256,
        queue_size: int = 2048,
        min_interval_ms: int = 0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
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
        # Per-symbol throttle (0 = off). The dict is created once; it holds one
        # int per subscribed symbol, like ``_latest_keys`` / ``_ring_keys``.
        self._skipped = 0
        self._min_interval_ns = max(0, int(min_interval_ms)) * 1_000_000
        self._clock_ns = clock_ns
        self._last_accept_ns: dict[str, int] = {}
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

    @property
    def skipped(self) -> int:
        return self._skipped

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
        # Per-symbol throttle: decided before any copy or serialization.
        interval_ns = self._min_interval_ns
        now_ns = 0
        if interval_ns:
            now_ns = self._clock_ns()
            last_ns = self._last_accept_ns.get(symbol)
            if last_ns is not None and now_ns - last_ns < interval_ns:
                self._skipped += 1
                return
        try:
            # Build a fresh normalized body and serialize while still holding
            # the GIL on the producer thread. ``list(...)`` creates a defensive
            # copy so the encoder's internal iteration cannot observe concurrent
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
        else:
            if interval_ns:
                self._last_accept_ns[symbol] = now_ns

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
