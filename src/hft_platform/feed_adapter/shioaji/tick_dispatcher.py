"""Tick dispatch pipeline extracted from ShioajiClient.

Owns the async quote dispatch queue + worker thread and the per-client
tick processing entry point.  All methods on the HOT PATH are
allocation-free (no list/dict creation, no f-string formatting).

Thread safety: ``_enqueue_tick`` is called from the broker callback
thread; the worker thread drains the queue and invokes ``_process_tick``
sequentially.

When ``HFT_TICK_RING_BUFFER=1``, the internal transport switches from
``queue.Queue`` (which acquires a mutex on every put/get) to a bounded
``collections.deque`` paired with a ``threading.Event`` for wake-up
notification.  ``deque.append`` / ``popleft`` are atomic in CPython
(GIL-protected C implementation) and avoid the per-operation Lock
overhead of ``queue.Queue``.
"""

from __future__ import annotations

import collections
import os
import queue
import threading
import time
from typing import Any, Callable

from structlog import get_logger

from hft_platform.feed_adapter.shioaji import router as _router

logger = get_logger("feed_adapter.tick_dispatcher")

_USE_RING_BUFFER: bool = os.environ.get("HFT_TICK_RING_BUFFER", "0") == "1"

# Type alias for queue items: (args, kwargs) or None (sentinel).
_Item = tuple[tuple[Any, ...], dict[str, Any]] | None


class TickDispatcher:
    """Manages the tick ingress queue and background dispatch worker.

    Constructor args
    ----------------
    process_tick_fn :
        The actual tick processing callback (typically bound
        ``ShioajiClient._process_tick_impl``).  Called on the worker
        thread (or inline when async dispatch is disabled).
    metrics :
        ``MetricsRegistry`` instance (may be ``None``).
    quote_dispatch_async :
        Whether to use the async queue+worker (``True``) or call
        ``process_tick_fn`` inline on the broker thread (``False``).
    queue_size :
        Max depth of the dispatch queue.
    batch_max :
        Max items drained per worker wake-up.
    metrics_every :
        Emit queue-depth metrics every N enqueued items.
    """

    __slots__ = (
        "_process_tick_fn",
        "_metrics",
        "_quote_dispatch_async",
        "_queue_size",
        "_batch_max",
        "_metrics_every",
        "_queue",
        "_deque",
        "_deque_event",
        "_thread",
        "_running",
        "_dropped",
        "_enqueued",
        "_processed",
        "_use_deque",
    )

    def __init__(
        self,
        process_tick_fn: Callable[..., Any],
        metrics: Any | None,
        *,
        quote_dispatch_async: bool = True,
        queue_size: int = 8192,
        batch_max: int = 32,
        metrics_every: int = 128,
        use_deque: bool | None = None,
    ) -> None:
        self._process_tick_fn = process_tick_fn
        self._metrics = metrics
        self._quote_dispatch_async = quote_dispatch_async
        self._queue_size = max(1, queue_size)
        self._batch_max = max(1, batch_max)
        self._metrics_every = max(1, metrics_every)
        # Legacy queue.Queue transport (default).
        self._queue: queue.Queue[_Item] | None = None
        # Lock-free deque transport (opt-in via env var or kwarg).
        self._deque: collections.deque[_Item] | None = None
        self._deque_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._running: bool = False
        self._dropped: int = 0
        self._enqueued: int = 0
        self._processed: int = 0
        self._use_deque: bool = use_deque if use_deque is not None else _USE_RING_BUFFER

    # ------------------------------------------------------------------
    # Public read-only counters (for diagnostics / backward compat)
    # ------------------------------------------------------------------

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def enqueued(self) -> int:
        return self._enqueued

    @property
    def processed(self) -> int:
        return self._processed

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # HOT PATH — enqueue_tick
    # ------------------------------------------------------------------

    def enqueue_tick(self, *args: Any, **kwargs: Any) -> None:
        """Non-blocking callback ingress: broker thread enqueues, worker executes."""
        metrics = self._metrics
        start_ns = time.perf_counter_ns() if metrics else 0
        try:
            if not self._quote_dispatch_async:
                self._process_tick_fn(*args, **kwargs)
                return
            self.start_worker()
            if self._use_deque:
                self._enqueue_deque(args, kwargs, metrics)
            else:
                self._enqueue_queue(args, kwargs, metrics)
        finally:
            if metrics and hasattr(metrics, "shioaji_quote_callback_ingress_latency_ns"):
                try:
                    metrics.shioaji_quote_callback_ingress_latency_ns.observe(max(0, time.perf_counter_ns() - start_ns))
                except Exception as _exc:  # noqa: BLE001
                    pass

    def _enqueue_queue(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        metrics: Any | None,
    ) -> None:
        """Enqueue via queue.Queue (legacy path)."""
        q = self._queue
        if q is None:
            self._process_tick_fn(*args, **kwargs)
            return
        try:
            q.put_nowait((args, kwargs))
            self._enqueued += 1
            if metrics and (self._enqueued % self._metrics_every == 0):
                try:
                    if hasattr(metrics, "shioaji_quote_callback_queue_depth"):
                        metrics.shioaji_quote_callback_queue_depth.set(q.qsize())
                except Exception as _exc:  # noqa: BLE001
                    pass
        except queue.Full:
            self._on_drop(metrics)

    def _enqueue_deque(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        metrics: Any | None,
    ) -> None:
        """Enqueue via bounded deque (lock-free path).

        ``deque.append`` with ``maxlen`` silently discards the oldest item
        when full.  To preserve the *drop newest* backpressure semantics
        of the original ``queue.Queue`` path, we check length first and
        drop the incoming item if the deque is at capacity.
        """
        dq = self._deque
        ev = self._deque_event
        if dq is None or ev is None:
            self._process_tick_fn(*args, **kwargs)
            return
        # len(deque) is O(1) and atomic in CPython.
        if len(dq) >= self._queue_size:
            self._on_drop(metrics)
            return
        # append is atomic in CPython (GIL-protected C call).
        dq.append((args, kwargs))
        self._enqueued += 1
        if metrics and (self._enqueued % self._metrics_every == 0):
            try:
                if hasattr(metrics, "shioaji_quote_callback_queue_depth"):
                    metrics.shioaji_quote_callback_queue_depth.set(len(dq))
            except Exception as _exc:  # noqa: BLE001
                pass
        # Wake the consumer.  ``Event.set()`` is very cheap when already set.
        ev.set()

    def _on_drop(self, metrics: Any | None) -> None:
        """Shared drop-handling for both transports."""
        self._dropped += 1
        if metrics:
            try:
                metrics.raw_queue_dropped_total.inc()
                if hasattr(metrics, "shioaji_quote_callback_queue_dropped_total"):
                    metrics.shioaji_quote_callback_queue_dropped_total.inc()
            except Exception as _exc:  # noqa: BLE001
                pass
        if self._dropped % 100 == 1:
            logger.warning(
                "Quote callback queue full; dropping quote callback payload",
                dropped_total=self._dropped,
                maxsize=self._queue_size,
            )

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def start_worker(self) -> None:
        """Start the background dispatch worker (idempotent)."""
        if not self._quote_dispatch_async or self._running:
            return
        if self._use_deque:
            self._start_deque_worker()
        else:
            self._start_queue_worker()

    def _start_queue_worker(self) -> None:
        """Spawn the legacy queue.Queue-based worker."""
        self._queue = queue.Queue(maxsize=self._queue_size)
        q = self._queue
        if q is None:  # pragma: no cover — defensive
            return
        self._running = True
        batch_max = self._batch_max
        process_fn = self._process_tick_fn
        metrics = self._metrics
        metrics_every = self._metrics_every

        def _worker() -> None:
            processed_local = 0
            while self._running:
                try:
                    item = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    continue
                batch_count = 0

                a, kw = item
                try:
                    process_fn(*a, **kw)
                except Exception as exc:
                    logger.error("Quote dispatch worker error", error=str(exc))
                batch_count += 1

                while batch_count < batch_max and self._running:
                    try:
                        nxt = q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        continue
                    na, nkw = nxt
                    try:
                        process_fn(*na, **nkw)
                    except Exception as exc:
                        logger.error("Quote dispatch worker error", error=str(exc))
                    batch_count += 1

                processed_local += batch_count
                self._processed = processed_local
                if metrics and (processed_local % metrics_every == 0):
                    try:
                        if hasattr(metrics, "shioaji_quote_callback_queue_depth"):
                            metrics.shioaji_quote_callback_queue_depth.set(q.qsize())
                    except Exception as _exc:  # noqa: BLE001
                        pass

        self._thread = threading.Thread(
            target=_worker,
            name="shioaji-quote-dispatch",
            daemon=True,
        )
        self._thread.start()

    def _start_deque_worker(self) -> None:
        """Spawn the lock-free deque-based worker."""
        # deque without maxlen — we enforce capacity in _enqueue_deque
        # to get drop-newest semantics instead of deque's drop-oldest.
        self._deque = collections.deque()
        self._deque_event = threading.Event()
        dq = self._deque
        ev = self._deque_event
        self._running = True
        batch_max = self._batch_max
        process_fn = self._process_tick_fn
        metrics = self._metrics
        metrics_every = self._metrics_every

        def _worker() -> None:
            processed_local = 0
            while self._running:
                # Wait for signal or timeout (avoids busy-spin).
                ev.wait(timeout=0.5)
                ev.clear()
                # Drain in batches until the deque is empty.  This avoids
                # stalling when more items than batch_max were enqueued
                # between a single Event.set() / .clear() pair.
                while self._running:
                    batch_count = 0
                    while batch_count < batch_max and self._running:
                        try:
                            item = dq.popleft()
                        except IndexError:
                            break
                        if item is None:
                            continue
                        a, kw = item
                        try:
                            process_fn(*a, **kw)
                        except Exception as exc:
                            logger.error("Quote dispatch worker error", error=str(exc))
                        batch_count += 1

                    processed_local += batch_count
                    self._processed = processed_local
                    if metrics and (processed_local % metrics_every == 0):
                        try:
                            if hasattr(metrics, "shioaji_quote_callback_queue_depth"):
                                metrics.shioaji_quote_callback_queue_depth.set(len(dq))
                        except Exception as _exc:  # noqa: BLE001
                            pass
                    # If this batch was smaller than batch_max, deque is
                    # empty — break out to the outer ev.wait() loop.
                    if batch_count < batch_max:
                        break

        self._thread = threading.Thread(
            target=_worker,
            name="shioaji-quote-dispatch-deque",
            daemon=True,
        )
        self._thread.start()

    def stop_worker(self, join_timeout_s: float = 1.0) -> None:
        """Gracefully stop the background dispatch worker."""
        if not self._running:
            return
        self._running = False
        # Wake the worker so it can exit promptly.
        if self._use_deque:
            ev = self._deque_event
            if ev is not None:
                ev.set()
        else:
            q = self._queue
            if q is not None:
                try:
                    q.put_nowait(None)
                except Exception as _exc:  # noqa: BLE001
                    pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=max(0.0, float(join_timeout_s)))
        self._thread = None
        self._queue = None
        self._deque = None
        self._deque_event = None

    # ------------------------------------------------------------------
    # Quote route refresh
    # ------------------------------------------------------------------

    @staticmethod
    def refresh_quote_routes(
        symbols: list[dict[str, Any]],
        subscribed_codes: set[str] | None,
        client: Any,
    ) -> None:
        """Sync the client's symbol codes into the global route registry."""
        codes: list[str] = []
        for sym in symbols:
            if isinstance(sym, dict):
                code = sym.get("code")
            else:
                code = None
            if code:
                codes.append(str(code))
        if subscribed_codes:
            codes.extend(str(c) for c in subscribed_codes)
        _router._registry_rebind_codes(client, codes)

    # ------------------------------------------------------------------
    # Wrapped tick callback (persistent, exception-safe)
    # ------------------------------------------------------------------

    @staticmethod
    def wrapped_tick_cb(tick_callback: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> None:
        """Persistent callback wrapper — swallows exceptions to avoid broker disconnects."""
        try:
            if tick_callback is not None:
                tick_callback(*args, **kwargs)
        except Exception as e:
            logger.error("Callback error", error=str(e))
