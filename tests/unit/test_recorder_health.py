"""Tests for hft_platform.recorder.health."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from hft_platform.recorder.health import PipelineHealthTracker, PipelineState

# ── PipelineState enum ───────────────────────────────────────────────


class TestPipelineState:
    def test_ordering(self) -> None:
        assert PipelineState.HEALTHY < PipelineState.DEGRADED
        assert PipelineState.DEGRADED < PipelineState.CRITICAL
        assert PipelineState.CRITICAL < PipelineState.DATA_LOSS

    def test_int_values(self) -> None:
        assert int(PipelineState.HEALTHY) == 0
        assert int(PipelineState.DEGRADED) == 1
        assert int(PipelineState.CRITICAL) == 2
        assert int(PipelineState.DATA_LOSS) == 3


# ── PipelineHealthTracker: init ──────────────────────────────────────


class TestPipelineHealthTrackerInit:
    def test_defaults(self) -> None:
        t = PipelineHealthTracker()
        assert t.state == PipelineState.HEALTHY
        assert t._ch_connected is True

    def test_env_override_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_HEALTH_WINDOW_S", "120")
        monkeypatch.setenv("HFT_HEALTH_CRITICAL_DISCONNECT_S", "90")
        t = PipelineHealthTracker()
        assert t._window_s == 120.0
        assert t._critical_disconnect_s == 90.0


# ── State transitions: DEGRADED ─────────────────────────────────────


class TestDegradedState:
    def test_wal_fallback_triggers_degraded(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("wal_fallback")
        assert t.state == PipelineState.DEGRADED

    def test_drop_triggers_degraded(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("drop")
        assert t.state == PipelineState.DEGRADED

    def test_recovery_after_window_expires(self) -> None:
        t = PipelineHealthTracker()
        t._window_s = 0.05  # 50ms window
        t.record_event("drop")
        assert t.state == PipelineState.DEGRADED
        time.sleep(0.1)
        # Record a benign event to trigger recompute
        t.record_event("ch_connected")
        assert t.state == PipelineState.HEALTHY


# ── State transitions: DATA_LOSS ─────────────────────────────────────


class TestDataLossState:
    def test_data_loss_event(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("data_loss")
        assert t.state == PipelineState.DATA_LOSS

    def test_data_loss_overrides_degraded(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("wal_fallback")
        assert t.state == PipelineState.DEGRADED
        t.record_event("data_loss")
        assert t.state == PipelineState.DATA_LOSS


# ── State transitions: CRITICAL ──────────────────────────────────────


class TestCriticalState:
    def test_ch_disconnect_plus_wal_fallback(self) -> None:
        t = PipelineHealthTracker()
        t._critical_disconnect_s = 0  # Instant critical threshold
        t.record_event("ch_disconnected")
        t.record_event("wal_fallback")
        assert t.state == PipelineState.CRITICAL

    def test_ch_disconnect_plus_ch_error(self) -> None:
        t = PipelineHealthTracker()
        t._critical_disconnect_s = 0
        t.record_event("ch_disconnected")
        t.record_event("ch_error")
        assert t.state == PipelineState.CRITICAL

    def test_ch_disconnect_alone_not_critical(self) -> None:
        """CH disconnect without wal_fallback/ch_warn stays at most DEGRADED."""
        t = PipelineHealthTracker()
        t._critical_disconnect_s = 0
        t.record_event("ch_disconnected")
        # No wal_fallback/ch_error events, so not CRITICAL
        assert t.state == PipelineState.HEALTHY

    def test_ch_reconnect_resets_disconnect_timer(self) -> None:
        t = PipelineHealthTracker()
        t._critical_disconnect_s = 0
        t.record_event("ch_disconnected")
        t.record_event("ch_error")
        assert t.state == PipelineState.CRITICAL
        # Reconnect
        t.record_event("ch_connected")
        # ch_error is still in window but CH is now connected, so duration=0
        # wal_fallback absent, drops absent => HEALTHY
        # But ch_error still counts as a ch_warn event in window...
        # Re-check: only CRITICAL if ch_down_duration > threshold AND (wal/ch_warn)
        # After reconnect ch_down_duration is 0, so not CRITICAL
        assert t.state != PipelineState.CRITICAL


# ── CH connectivity tracking ─────────────────────────────────────────


class TestChConnectivity:
    def test_ch_timeout_disconnects(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("ch_timeout")
        assert t._ch_connected is False

    def test_ch_error_disconnects(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("ch_error")
        assert t._ch_connected is False

    def test_ch_disconnected_event(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("ch_disconnected")
        assert t._ch_connected is False

    def test_ch_connected_reconnects(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("ch_disconnected")
        assert t._ch_connected is False
        t.record_event("ch_connected")
        assert t._ch_connected is True


# ── get_health ───────────────────────────────────────────────────────


class TestGetHealth:
    def test_healthy_snapshot(self) -> None:
        t = PipelineHealthTracker()
        h = t.get_health()
        assert h["state"] == "HEALTHY"
        assert h["state_value"] == 0
        assert h["ch_connected"] is True
        assert h["events_in_window"] == 0
        assert isinstance(h["event_counts"], dict)

    def test_event_counts_populated(self) -> None:
        t = PipelineHealthTracker()
        t.record_event("drop")
        t.record_event("drop")
        t.record_event("wal_fallback")
        h = t.get_health()
        assert h["event_counts"]["drop"] == 2
        assert h["event_counts"]["wal_fallback"] == 1
        assert h["events_in_window"] == 3

    def test_window_s_included(self) -> None:
        t = PipelineHealthTracker()
        h = t.get_health()
        assert h["window_s"] == t._window_s

    def test_get_health_expires_stale_degraded_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [100.0]
        monkeypatch.setattr("hft_platform.recorder.health.time.monotonic", lambda: now[0])

        tracker = PipelineHealthTracker()
        tracker._metrics = None
        tracker._window_s = 1.0

        tracker.record_event("wal_fallback", table="audit.guardrail_log", count=1)
        assert tracker.get_health()["state"] == "DEGRADED"

        now[0] = 102.0
        health = tracker.get_health()

        assert health["state"] == "HEALTHY"
        assert health["state_value"] == 0
        assert health["events_in_window"] == 0


# ── prune ────────────────────────────────────────────────────────────


class TestPrune:
    def test_prune_removes_old_events(self) -> None:
        t = PipelineHealthTracker()
        t._window_s = 0.05  # 50ms
        t.record_event("drop")
        time.sleep(0.1)
        t.prune()
        assert len(t._events) == 0

    def test_prune_keeps_recent_events(self) -> None:
        t = PipelineHealthTracker()
        t._window_s = 60  # Large window
        t.record_event("drop")
        t.prune()
        assert len(t._events) == 1

    def test_prune_partial(self) -> None:
        t = PipelineHealthTracker()
        t._window_s = 0.05
        t.record_event("drop")
        time.sleep(0.1)
        t.record_event("wal_fallback")  # Recent
        t.prune()
        assert len(t._events) == 1


# ── Metrics integration ─────────────────────────────────────────────


class TestMetrics:
    def test_no_metrics_does_not_crash(self) -> None:
        """If MetricsRegistry is unavailable, state transitions still work."""
        t = PipelineHealthTracker()
        t._metrics = None
        t.record_event("data_loss")
        assert t.state == PipelineState.DATA_LOSS

    def test_metrics_updated_on_transition(self) -> None:
        t = PipelineHealthTracker()
        mock_metrics = type(
            "M",
            (),
            {
                "pipeline_health_state": type("G", (), {"set": lambda self, v: None})(),
                "pipeline_degradation_events_total": type("C", (), {"inc": lambda self: None})(),
            },
        )()
        t._metrics = mock_metrics
        with (
            patch.object(mock_metrics.pipeline_health_state, "set") as mock_set,
            patch.object(mock_metrics.pipeline_degradation_events_total, "inc") as mock_inc,
        ):
            t.record_event("data_loss")
            mock_set.assert_called_once_with(int(PipelineState.DATA_LOSS))
            mock_inc.assert_called_once()


# ── Edge: deque maxlen ───────────────────────────────────────────────


class TestDequeMaxlen:
    def test_events_bounded_at_1000(self) -> None:
        t = PipelineHealthTracker()
        for i in range(1100):
            t.record_event("drop")
        assert len(t._events) == 1000
