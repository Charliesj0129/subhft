"""Tests for QuoteEventHandler, QuotePendingState, and QuoteRuntimeSnapshot."""

from __future__ import annotations

import dataclasses

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
