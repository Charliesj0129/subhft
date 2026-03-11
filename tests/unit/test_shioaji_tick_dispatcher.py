"""Tests for TickDispatcher — tick ingress queue + worker lifecycle."""

from __future__ import annotations

import queue
import time
from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.tick_dispatcher import TickDispatcher


# ------------------------------------------------------------------
# _process_tick callback routing
# ------------------------------------------------------------------


class TestProcessTickRouting:
    """Verify that enqueue_tick ultimately invokes the process_tick_fn."""

    def test_sync_dispatch_calls_process_tick_directly(self) -> None:
        """When async dispatch is disabled, process_tick_fn is called inline."""
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=False,
        )
        dispatcher.enqueue_tick("topic", "quote_obj", extra=42)
        cb.assert_called_once_with("topic", "quote_obj", extra=42)

    def test_async_dispatch_calls_process_tick_on_worker(self) -> None:
        """When async dispatch is enabled, process_tick_fn runs on worker thread."""
        received: list[tuple] = []

        def _capture(*args: object, **kwargs: object) -> None:
            received.append((args, kwargs))

        dispatcher = TickDispatcher(
            process_tick_fn=_capture,
            metrics=None,
            quote_dispatch_async=True,
            queue_size=64,
        )
        dispatcher.enqueue_tick("topic", "quote")
        # Allow worker to drain.
        time.sleep(0.2)
        dispatcher.stop_worker()

        assert len(received) == 1
        assert received[0] == (("topic", "quote"), {})

    def test_process_tick_exception_does_not_crash_worker(self) -> None:
        """Errors inside process_tick_fn are caught; worker keeps running."""
        call_count = 0

        def _flaky(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")

        dispatcher = TickDispatcher(
            process_tick_fn=_flaky,
            metrics=None,
            quote_dispatch_async=True,
            queue_size=64,
        )
        dispatcher.enqueue_tick("a")
        time.sleep(0.15)
        dispatcher.enqueue_tick("b")
        time.sleep(0.15)
        dispatcher.stop_worker()
        assert call_count == 2


# ------------------------------------------------------------------
# Enqueue / dequeue behaviour
# ------------------------------------------------------------------


class TestEnqueueDequeue:
    def test_drop_when_queue_full(self) -> None:
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=True,
            queue_size=1,
        )
        dispatcher.start_worker()
        # Replace queue with a pre-filled one.
        dispatcher._queue = queue.Queue(maxsize=1)
        dispatcher._queue.put_nowait(((), {}))

        dispatcher.enqueue_tick("Q/TSE/2330")
        assert dispatcher.dropped == 1
        cb.assert_not_called()
        dispatcher.stop_worker()

    def test_enqueue_increments_counter(self) -> None:
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=True,
            queue_size=64,
        )
        dispatcher.enqueue_tick("a")
        dispatcher.enqueue_tick("b")
        assert dispatcher.enqueued == 2
        time.sleep(0.15)
        dispatcher.stop_worker()

    def test_fallback_to_inline_when_queue_none(self) -> None:
        """If _queue is None despite async=True, falls back to inline call."""
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=True,
            queue_size=64,
        )
        # Force queue to None (simulates race before worker init).
        dispatcher._running = True
        dispatcher._queue = None
        dispatcher.enqueue_tick("tick_data")
        cb.assert_called_once_with("tick_data")
        dispatcher._running = False

    def test_batch_drain(self) -> None:
        """Worker drains up to batch_max items per wake-up."""
        received: list[str] = []

        def _capture(*args: object, **kwargs: object) -> None:
            received.append(args[0])  # type: ignore[arg-type]

        dispatcher = TickDispatcher(
            process_tick_fn=_capture,
            metrics=None,
            quote_dispatch_async=True,
            queue_size=64,
            batch_max=4,
        )
        for i in range(6):
            dispatcher.enqueue_tick(f"t{i}")
        time.sleep(0.3)
        dispatcher.stop_worker()
        assert received == [f"t{i}" for i in range(6)]
        assert dispatcher.processed == 6


# ------------------------------------------------------------------
# Worker lifecycle
# ------------------------------------------------------------------


class TestWorkerLifecycle:
    def test_start_stop(self) -> None:
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=True,
        )
        dispatcher.start_worker()
        assert dispatcher.running is True
        assert dispatcher._thread is not None
        assert dispatcher._thread.is_alive()

        dispatcher.stop_worker()
        assert dispatcher.running is False
        assert dispatcher._thread is None
        assert dispatcher._queue is None

    def test_start_is_idempotent(self) -> None:
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=True,
        )
        dispatcher.start_worker()
        thread1 = dispatcher._thread
        dispatcher.start_worker()
        thread2 = dispatcher._thread
        assert thread1 is thread2
        dispatcher.stop_worker()

    def test_stop_is_idempotent(self) -> None:
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=True,
        )
        # Calling stop without start should not raise.
        dispatcher.stop_worker()
        dispatcher.stop_worker()

    def test_no_worker_when_async_disabled(self) -> None:
        cb = MagicMock()
        dispatcher = TickDispatcher(
            process_tick_fn=cb,
            metrics=None,
            quote_dispatch_async=False,
        )
        dispatcher.start_worker()
        assert dispatcher.running is False
        assert dispatcher._thread is None


# ------------------------------------------------------------------
# refresh_quote_routes
# ------------------------------------------------------------------


class TestRefreshQuoteRoutes:
    def test_routes_synced_from_symbols(self) -> None:
        from hft_platform.feed_adapter.shioaji import router

        client = MagicMock()
        symbols = [{"code": "2330", "exchange": "TSE"}, {"code": "2317", "exchange": "OTC"}]
        with router.CLIENT_REGISTRY_LOCK:
            router.CLIENT_REGISTRY.clear()
            router.CLIENT_REGISTRY_BY_CODE.clear()
        router._registry_register(client)

        TickDispatcher.refresh_quote_routes(symbols, None, client)

        snapshot = router.CLIENT_REGISTRY_BY_CODE_SNAPSHOT
        assert "2330" in snapshot
        assert "2317" in snapshot

    def test_routes_include_subscribed_codes(self) -> None:
        from hft_platform.feed_adapter.shioaji import router

        client = MagicMock()
        with router.CLIENT_REGISTRY_LOCK:
            router.CLIENT_REGISTRY.clear()
            router.CLIENT_REGISTRY_BY_CODE.clear()
        router._registry_register(client)

        TickDispatcher.refresh_quote_routes([], {"AAAA"}, client)
        snapshot = router.CLIENT_REGISTRY_BY_CODE_SNAPSHOT
        assert "AAAA" in snapshot


# ------------------------------------------------------------------
# wrapped_tick_cb
# ------------------------------------------------------------------


class TestWrappedTickCb:
    def test_calls_callback(self) -> None:
        cb = MagicMock()
        TickDispatcher.wrapped_tick_cb(cb, "a", "b", k=1)
        cb.assert_called_once_with("a", "b", k=1)

    def test_noop_when_callback_none(self) -> None:
        # Should not raise.
        TickDispatcher.wrapped_tick_cb(None, "a")

    def test_exception_swallowed(self) -> None:
        cb = MagicMock(side_effect=RuntimeError("oops"))
        # Should not raise.
        TickDispatcher.wrapped_tick_cb(cb, "a")
        cb.assert_called_once()
