"""Tests for QuoteEventHandler, QuotePendingState, and QuoteRuntimeSnapshot."""

from __future__ import annotations

import dataclasses
import threading

import pytest

from hft_platform.feed_adapter.shioaji.quote_runtime import (
    QuoteEventHandler,
    QuotePendingState,
    QuoteRuntimeSnapshot,
)


class TestQuotePendingState:
    def test_frozen_dataclass(self) -> None:
        state = QuotePendingState(pending=True, reason="no_data", ts=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            state.pending = False  # type: ignore[misc]

    def test_fields(self) -> None:
        state = QuotePendingState(pending=True, reason="event_12", ts=42.5)
        assert state.pending is True
        assert state.reason == "event_12"
        assert state.ts == 42.5


class TestQuoteRuntimeSnapshot:
    def test_frozen_dataclass(self) -> None:
        snap = QuoteRuntimeSnapshot(
            pending_resubscribe=False,
            pending_reason=None,
            pending_since=0.0,
            callbacks_registered=True,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.pending_resubscribe = True  # type: ignore[misc]

    def test_fields(self) -> None:
        snap = QuoteRuntimeSnapshot(
            pending_resubscribe=True,
            pending_reason="no_data",
            pending_since=100.0,
            callbacks_registered=False,
        )
        assert snap.pending_resubscribe is True
        assert snap.pending_reason == "no_data"
        assert snap.pending_since == 100.0
        assert snap.callbacks_registered is False


class TestQuoteEventHandler:
    def test_initial_state_not_pending(self) -> None:
        handler = QuoteEventHandler()
        assert handler.is_pending is False
        assert handler.current_reason is None
        assert handler.pending_since == 0.0

    def test_mark_pending_transitions_to_pending(self) -> None:
        handler = QuoteEventHandler()
        result = handler.mark_pending("no_data", current_ts=10.0)
        assert result.pending is True
        assert result.reason == "no_data"
        assert result.ts == 10.0
        assert handler.is_pending is True
        assert handler.current_reason == "no_data"
        assert handler.pending_since == 10.0

    def test_mark_pending_idempotent_same_reason(self) -> None:
        handler = QuoteEventHandler()
        first = handler.mark_pending("no_data", current_ts=10.0)
        second = handler.mark_pending("no_data", current_ts=20.0)
        assert second.pending is True
        assert second.reason == "no_data"
        assert second.ts == first.ts
        assert handler.pending_since == 10.0

    def test_mark_pending_different_reason_updates(self) -> None:
        handler = QuoteEventHandler()
        handler.mark_pending("no_data", current_ts=10.0)
        result = handler.mark_pending("event_12", current_ts=20.0)
        assert result.pending is True
        assert result.reason == "event_12"
        assert result.ts == 20.0
        assert handler.current_reason == "event_12"
        assert handler.pending_since == 20.0

    def test_mark_pending_with_explicit_current_ts(self) -> None:
        handler = QuoteEventHandler()
        result = handler.mark_pending("watchdog", current_ts=99.9)
        assert result.ts == 99.9
        assert handler.pending_since == 99.9

    def test_mark_pending_without_explicit_ts_uses_timebase(self) -> None:
        handler = QuoteEventHandler()
        result = handler.mark_pending("no_data")
        assert result.ts > 0.0
        assert handler.pending_since == result.ts

    def test_clear_pending_transitions_to_not_pending(self) -> None:
        handler = QuoteEventHandler()
        handler.mark_pending("no_data", current_ts=10.0)
        result = handler.clear_pending()
        assert result.pending is False
        assert result.reason is None
        assert result.ts == 0.0
        assert handler.is_pending is False
        assert handler.current_reason is None
        assert handler.pending_since == 0.0

    def test_clear_pending_idempotent_when_already_clear(self) -> None:
        handler = QuoteEventHandler()
        result = handler.clear_pending()
        assert result.pending is False
        assert result.reason is None
        assert result.ts == 0.0
        assert handler.is_pending is False

    def test_is_pending_property(self) -> None:
        handler = QuoteEventHandler()
        assert handler.is_pending is False
        handler.mark_pending("reason_a", current_ts=1.0)
        assert handler.is_pending is True
        handler.clear_pending()
        assert handler.is_pending is False

    def test_current_reason_property(self) -> None:
        handler = QuoteEventHandler()
        assert handler.current_reason is None
        handler.mark_pending("alpha", current_ts=1.0)
        assert handler.current_reason == "alpha"
        handler.mark_pending("beta", current_ts=2.0)
        assert handler.current_reason == "beta"

    def test_pending_since_property(self) -> None:
        handler = QuoteEventHandler()
        assert handler.pending_since == 0.0
        handler.mark_pending("x", current_ts=55.5)
        assert handler.pending_since == 55.5

    def test_snapshot_when_not_pending(self) -> None:
        handler = QuoteEventHandler()
        snap = handler.snapshot()
        assert snap.pending is False
        assert snap.reason is None
        assert snap.ts == 0.0

    def test_snapshot_when_pending(self) -> None:
        handler = QuoteEventHandler()
        handler.mark_pending("event_12", current_ts=77.7)
        snap = handler.snapshot()
        assert snap.pending is True
        assert snap.reason == "event_12"
        assert snap.ts == 77.7

    def test_pending_lock_is_rlock(self) -> None:
        """P0-D2: lock must be reentrant because QuoteRuntime.mark_pending
        acquires pending_lock and then calls handler.mark_pending which
        re-acquires the same lock from the same thread."""
        handler = QuoteEventHandler()
        lock = handler.pending_lock
        # threading.RLock() exposes acquire/release and is reentrant — test by
        # double-acquiring on the same thread.
        assert lock.acquire(blocking=False)
        try:
            assert lock.acquire(blocking=False), (
                "pending_lock must be reentrant so QuoteRuntime wrappers can "
                "nest with handler.mark_pending without deadlock"
            )
            lock.release()
        finally:
            lock.release()

    def test_concurrent_mark_clear_never_torn(self) -> None:
        """P0-D2 regression: (_pending_reason, _pending_ts) must never be
        observable as ``reason is None and ts > 0`` or ``reason is not None
        and ts == 0``. Drive mark + clear from multiple threads and sample
        the state mid-flight via ``snapshot()``."""
        handler = QuoteEventHandler()
        stop = threading.Event()
        torn: list[tuple[str | None, float]] = []

        def marker() -> None:
            tick = 0
            while not stop.is_set() and tick < 5_000:
                tick += 1
                handler.mark_pending(f"reason_{tick % 7}", current_ts=float(tick))

        def clearer() -> None:
            tick = 0
            while not stop.is_set() and tick < 5_000:
                tick += 1
                handler.clear_pending()

        def sampler() -> None:
            sampled = 0
            while not stop.is_set() and sampled < 5_000:
                sampled += 1
                snap = handler.snapshot()
                # Invariant: pending=True iff reason is not None and ts > 0.
                # pending=False iff reason is None and ts == 0.
                if snap.pending:
                    if snap.reason is None or snap.ts == 0.0:
                        torn.append((snap.reason, snap.ts))
                        return
                else:
                    if snap.reason is not None or snap.ts != 0.0:
                        torn.append((snap.reason, snap.ts))
                        return

        threads = [
            threading.Thread(target=marker, daemon=True),
            threading.Thread(target=clearer, daemon=True),
            threading.Thread(target=sampler, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        stop.set()

        assert not torn, f"found torn snapshots: first={torn[0]}"

    def test_full_lifecycle(self) -> None:
        handler = QuoteEventHandler()
        assert handler.is_pending is False
        s1 = handler.mark_pending("no_data", current_ts=10.0)
        assert s1.pending is True
        assert s1.reason == "no_data"
        s2 = handler.clear_pending()
        assert s2.pending is False
        assert handler.is_pending is False
        s3 = handler.mark_pending("event_12", current_ts=30.0)
        assert s3.pending is True
        assert s3.reason == "event_12"
        assert s3.ts == 30.0
        assert handler.current_reason == "event_12"


class TestSubRetryCapacityAwareness:
    """Verify retry thread stops when subscription capacity is reached."""

    def test_retry_stops_at_capacity(self) -> None:
        """Retry loop should break immediately when subscribed_count >= MAX_SUBSCRIPTIONS."""
        import unittest.mock as mock

        from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime

        client = mock.MagicMock()
        client.MAX_SUBSCRIPTIONS = 120
        client.subscribed_count = 120
        # Must be False initially so start_sub_retry_thread doesn't early-return
        client._sub_retry_running = False
        client._failed_sub_symbols = [
            {"code": "TXO35050D6", "exchange": "OPT"},
            {"code": "TXO35100D6", "exchange": "OPT"},
        ]
        client._contract_retry_s = 0.01
        client.logged_in = True
        client._callbacks_registered = True
        client._event_callback_registered = True
        client._quote_api.return_value = mock.MagicMock()
        client._set_thread_alive_metric = mock.MagicMock()

        runtime = QuoteRuntime(client)
        runtime.start_sub_retry_thread(mock.MagicMock())

        if client._sub_retry_thread and client._sub_retry_thread.is_alive():
            client._sub_retry_thread.join(timeout=2.0)

        assert client._sub_retry_running is False
        client._subscribe_symbol.assert_not_called()

    def test_retry_proceeds_when_under_capacity(self) -> None:
        """Retry loop should attempt resubscription when under capacity."""
        import unittest.mock as mock

        from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime

        client = mock.MagicMock()
        client.MAX_SUBSCRIPTIONS = 120
        client.subscribed_count = 100
        client._sub_retry_running = False
        client._failed_sub_symbols = [
            {"code": "TXO35050D6", "exchange": "OPT"},
        ]
        client._contract_retry_s = 0.01
        client.logged_in = True
        client._callbacks_registered = True
        client._event_callback_registered = True
        client._quote_api.return_value = mock.MagicMock()
        client._set_thread_alive_metric = mock.MagicMock()

        def subscribe_and_succeed(sym: object, cb: object) -> bool:
            client.subscribed_count += 1
            return True

        client._subscribe_symbol = mock.MagicMock(side_effect=subscribe_and_succeed)

        runtime = QuoteRuntime(client)
        runtime.start_sub_retry_thread(mock.MagicMock())

        if client._sub_retry_thread and client._sub_retry_thread.is_alive():
            client._sub_retry_thread.join(timeout=2.0)

        assert client._subscribe_symbol.call_count >= 1
        assert client._sub_retry_running is False
