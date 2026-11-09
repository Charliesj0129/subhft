"""A code queued while the subscription retry worker is exiting must still be retried.

Codex review (2026-11-09): the worker saw an empty queue and logged "All failed
subscriptions resolved"; before it cleared ``_sub_retry_running`` a universe roll
appended a refused code and called ``start_sub_retry_thread``, which returned
because the flag was still set. The worker then cleared the flag and exited,
and the code sat in the queue with nobody to retry it. The start and the exit
now share ``QuoteRuntime._sub_retry_lock``.
"""

from __future__ import annotations

import collections
from types import SimpleNamespace
from typing import Any

import pytest

from hft_platform.feed_adapter.shioaji import quote_runtime as qr
from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime


def _client() -> SimpleNamespace:
    client = SimpleNamespace(
        _sub_retry_running=False,
        _failed_sub_symbols=collections.deque(),
        _contract_retry_s=0.0,
        logged_in=True,
        _callbacks_registered=True,
        _event_callback_registered=True,
        tick_callback=None,
        subscribed_codes=set(),
        subscribed_count=0,
        MAX_SUBSCRIPTIONS=100,
        subscribed=[],
        metrics=None,
    )
    client._quote_api = lambda: SimpleNamespace(subscribe=lambda *a, **k: None)
    client._set_thread_alive_metric = lambda name, alive: None

    def subscribe(sym: dict[str, Any], cb: Any) -> bool:
        client.subscribed.append(sym["code"])
        return True

    client._subscribe_symbol = subscribe
    return client


class _Logger:
    """Injects the race at the exact point the worker decides its queue is empty."""

    def __init__(self, on_resolved: Any) -> None:
        self._on_resolved = on_resolved
        self.fired = 0

    def info(self, event: str, **kwargs: Any) -> None:
        if event == "All failed subscriptions resolved" and not self.fired:
            self.fired += 1
            self._on_resolved()

    def warning(self, event: str = "", **kwargs: Any) -> None:
        if event == "Subscription retry stopped: capacity reached" and not self.fired:
            self.fired += 1
            self._on_resolved()

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass


def test_code_queued_while_the_worker_exits_is_still_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    runtime = QuoteRuntime(client)  # type: ignore[arg-type]  # a duck-typed client is enough for the worker
    starts_refused: list[bool] = []

    def roll_appends_and_starts() -> None:
        client._failed_sub_symbols.append({"code": "NEW"})
        before = client._sub_retry_running
        runtime.start_sub_retry_thread(lambda *a: None)
        starts_refused.append(before and client._sub_retry_running)

    monkeypatch.setattr(qr, "logger", _Logger(roll_appends_and_starts))
    client._failed_sub_symbols.append({"code": "OLD"})

    runtime.start_sub_retry_thread(lambda *a: None)
    client._sub_retry_thread.join(timeout=5)

    assert not client._sub_retry_thread.is_alive()
    assert starts_refused == [True]  # the race happened: the second start saw a running worker
    assert client.subscribed == ["OLD", "NEW"]
    assert not client._failed_sub_symbols
    assert client._sub_retry_running is False


def test_capacity_stop_still_exits_with_codes_left(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    client.MAX_SUBSCRIPTIONS = 0
    runtime = QuoteRuntime(client)  # type: ignore[arg-type]  # a duck-typed client is enough for the worker
    client._failed_sub_symbols.append({"code": "FULL"})

    runtime.start_sub_retry_thread(lambda *a: None)
    client._sub_retry_thread.join(timeout=5)

    assert not client._sub_retry_thread.is_alive()
    assert client._sub_retry_running is False
    assert client.subscribed == []


def test_capacity_freed_while_the_worker_exits_on_capacity_is_still_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker stopped on a full connection; a roll then freed a slot and queued a code before the worker left."""
    client = _client()
    client.MAX_SUBSCRIPTIONS = 1
    client.subscribed_codes = {"SETTLED"}
    client.subscribed_count = 1
    runtime = QuoteRuntime(client)  # type: ignore[arg-type]  # a duck-typed client is enough for the worker

    attempts = {"n": 0}

    def subscribe(sym: dict[str, Any], cb: Any) -> bool:
        attempts["n"] += 1
        if attempts["n"] == 1:  # the first pass fills the last slot's competitor: keep FULL queued
            client.subscribed_count = client.MAX_SUBSCRIPTIONS
            return False
        client.subscribed.append(sym["code"])
        return True

    client._subscribe_symbol = subscribe

    def roll_frees_a_slot_and_queues() -> None:
        client.subscribed_codes.discard("SETTLED")
        client.subscribed_count = 0
        client._failed_sub_symbols.append({"code": "NEW"})
        runtime.start_sub_retry_thread(lambda *a: None)  # returns: the worker still looks alive

    monkeypatch.setattr(qr, "logger", _Logger(roll_frees_a_slot_and_queues))
    client._failed_sub_symbols.append({"code": "FULL"})
    client.subscribed_count = 0  # room for one attempt on the first pass

    runtime.start_sub_retry_thread(lambda *a: None)
    client._sub_retry_thread.join(timeout=5)

    assert not client._sub_retry_thread.is_alive()
    assert "NEW" in client.subscribed
    assert client._sub_retry_running is False


def test_a_queued_code_no_longer_in_the_facade_symbols_is_dropped_not_retried() -> None:
    """A universe roll removed the code after it was queued: the worker must not spend a slot on it."""
    client = _client()
    client.symbols = [{"code": "NEW"}]
    runtime = QuoteRuntime(client)  # type: ignore[arg-type]  # a duck-typed client is enough for the worker
    client._failed_sub_symbols.extend([{"code": "OLD"}, {"code": "NEW"}])

    runtime.start_sub_retry_thread(lambda *a: None)
    client._sub_retry_thread.join(timeout=5)

    assert client.subscribed == ["NEW"]
    assert not client._failed_sub_symbols


def test_roll_waits_for_an_in_flight_retry_and_then_removes_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex round 9: a retry mid-subscribe of OLD while a roll swaps OLD for NEW left OLD subscribed for good."""
    import threading

    from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

    client = _client()
    client.symbols = [{"code": "OLD"}]
    in_subscribe = threading.Event()
    release = threading.Event()
    unsubscribed: list[str] = []

    def slow_subscribe(sym: dict[str, Any], cb: Any) -> bool:
        if sym["code"] == "OLD":
            in_subscribe.set()
            assert release.wait(timeout=5)
        client.subscribed.append(sym["code"])
        return True

    client._subscribe_symbol = slow_subscribe
    client._subscriptions = lambda: SimpleNamespace(
        _unsubscribe_symbol=lambda sym: unsubscribed.append(sym["code"]),
        _subscribe_symbol=slow_subscribe,
    )
    client._load_config = lambda: setattr(client, "symbols", [{"code": "NEW"}])
    client._refresh_quote_routes = lambda: None
    runtime = QuoteRuntime(client)  # type: ignore[arg-type]  # a duck-typed client is enough for the worker
    client._failed_sub_symbols.append({"code": "OLD"})
    runtime.start_sub_retry_thread(lambda *a: None)
    assert in_subscribe.wait(timeout=5)

    pool: Any = SimpleNamespace(_user_callback=lambda *a: None, _slots=[], _make_callback_wrapper=lambda s, cb: cb)
    plan = SimpleNamespace(removed={0: ["OLD"]}, added={0: ["NEW"]})
    facade = SimpleNamespace(logged_in=True, _client=client)
    roll = threading.Thread(target=QuoteConnectionPool._apply_universe_to_facade, args=(pool, 0, facade, plan))
    roll.start()
    # Bounded 50 ms wait (AGENTS: <= 50 ms when unavoidable): proving a thread is blocked needs some time to pass;
    # the retry is parked on ``release`` and cannot finish, so this cannot pass by luck when the lock is missing.
    roll.join(timeout=0.05)
    assert roll.is_alive()  # the roll waits for the in-flight retry instead of interleaving with it

    release.set()
    roll.join(timeout=5)
    client._sub_retry_thread.join(timeout=5)

    assert unsubscribed == ["OLD"]
    assert client.subscribed_codes == {"NEW"}
