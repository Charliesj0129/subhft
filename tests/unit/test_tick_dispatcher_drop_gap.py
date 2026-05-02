"""H12: TickDispatcher drop callback must propagate gap signal.

Root cause: when the queue.Queue backing transport is full, ticks are
silently dropped. FeatureEngine/LOBEngine cannot observe the gap and
rolling OFI/EMA state silently decays against missing flow. Strategies
cannot distinguish 'no new info' from 'lost N ticks'.

Fix: a caller-provided on_drop_callback that is invoked once per drop.
Bootstrapped in MarketDataService to call FeatureEngine.mark_gap_all so
downstream feature updates carry QUALITY_FLAG_GAP.
"""

from __future__ import annotations

from hft_platform.feed_adapter.shioaji.tick_dispatcher import TickDispatcher


def _stub_process_tick(*_args, **_kwargs):
    pass


def test_on_drop_callback_fires_when_queue_full():
    calls: list[int] = []

    dispatcher = TickDispatcher(
        process_tick_fn=_stub_process_tick,
        metrics=None,
        quote_dispatch_async=True,
        queue_size=1,
        batch_max=1,
    )
    dispatcher.set_on_drop_callback(lambda: calls.append(1))

    # Force queue-backed path, bypass worker thread so the single slot
    # stays occupied after the first enqueue.
    dispatcher._deque = None
    dispatcher._use_deque = False
    import queue as _queue

    dispatcher._queue = _queue.Queue(maxsize=1)
    dispatcher._running = True  # skip start_worker spawning a thread

    dispatcher.enqueue_tick("a")
    dispatcher.enqueue_tick("b")  # expected to drop
    dispatcher.enqueue_tick("c")  # expected to drop

    assert dispatcher.dropped == 2
    assert len(calls) == 2


def test_on_drop_callback_exceptions_do_not_propagate():
    def _raising():
        raise RuntimeError("boom")

    dispatcher = TickDispatcher(
        process_tick_fn=_stub_process_tick,
        metrics=None,
        quote_dispatch_async=True,
        queue_size=1,
    )
    dispatcher.set_on_drop_callback(_raising)

    dispatcher._deque = None
    dispatcher._use_deque = False
    import queue as _queue

    dispatcher._queue = _queue.Queue(maxsize=1)
    dispatcher._running = True

    dispatcher.enqueue_tick("a")
    # Must not raise despite the callback raising.
    dispatcher.enqueue_tick("b")
    assert dispatcher.dropped == 1


def test_no_callback_is_safe():
    dispatcher = TickDispatcher(
        process_tick_fn=_stub_process_tick,
        metrics=None,
        quote_dispatch_async=True,
        queue_size=1,
    )
    dispatcher._deque = None
    dispatcher._use_deque = False
    import queue as _queue

    dispatcher._queue = _queue.Queue(maxsize=1)
    dispatcher._running = True

    dispatcher.enqueue_tick("a")
    dispatcher.enqueue_tick("b")  # drop, no callback — must not crash
    assert dispatcher.dropped == 1
