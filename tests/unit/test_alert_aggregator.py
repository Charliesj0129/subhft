"""Tests for alert dedup and time-window aggregation."""
from __future__ import annotations

import pytest

from hft_platform.notifications.alert import Alert, AlertSeverity


def _make_alert(
    *,
    dedup_key: str | None = "test_key",
    severity: AlertSeverity = AlertSeverity.WARN,
    ts_ns: int = 1_000_000_000_000_000_000,
    alert_id: str = "a-001",
) -> Alert:
    return Alert(
        alert_id=alert_id, severity=severity, category="feed", source="test",
        title="Test alert", detail="Details here", ts_ns=ts_ns,
        dedup_key=dedup_key, metadata=None,
    )


class TestAlertAggregator:
    def test_first_alert_passes_through(self):
        from hft_platform.notifications.aggregator import AlertAggregator
        agg = AlertAggregator(window_ns=300_000_000_000)
        alert = _make_alert()
        result = agg.process(alert)
        assert result is not None
        assert result.alert_id == "a-001"

    def test_duplicate_within_window_is_suppressed(self):
        from hft_platform.notifications.aggregator import AlertAggregator
        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(ts_ns=1_000_000_000_000_000_000)
        # a2 arrives 1 second later — well within the 300-second window
        a2 = _make_alert(ts_ns=1_000_000_001_000_000_000, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is None

    def test_alert_after_window_passes_through(self):
        from hft_platform.notifications.aggregator import AlertAggregator
        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(ts_ns=1_000_000_000_000_000_000)
        # a2 arrives 301 seconds later — just past the 300-second window
        a2 = _make_alert(ts_ns=1_000_000_301_000_000_000, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None

    def test_none_dedup_key_always_passes(self):
        from hft_platform.notifications.aggregator import AlertAggregator
        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(dedup_key=None, ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(dedup_key=None, ts_ns=1_000_000_000_000_000_001, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None

    def test_fatal_never_aggregated(self):
        from hft_platform.notifications.aggregator import AlertAggregator
        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(severity=AlertSeverity.FATAL, ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(severity=AlertSeverity.FATAL, ts_ns=1_000_000_000_000_000_001, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None

    def test_flush_pending_returns_summary(self):
        from hft_platform.notifications.aggregator import AlertAggregator
        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(ts_ns=1_000_000_000_000_000_000)
        # a2 and a3 arrive within the 300-second window (1s and 2s after a1)
        a2 = _make_alert(ts_ns=1_000_000_001_000_000_000, alert_id="a-002")
        a3 = _make_alert(ts_ns=1_000_000_002_000_000_000, alert_id="a-003")
        agg.process(a1)
        agg.process(a2)
        agg.process(a3)
        # flush after window expires: now_ns > window_end = a1.ts_ns + 300_000_000_000
        summaries = agg.flush_expired(now_ns=1_000_000_301_000_000_000)
        assert len(summaries) == 1
        assert summaries[0].suppressed_count == 2

    def test_different_dedup_keys_independent(self):
        from hft_platform.notifications.aggregator import AlertAggregator
        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(dedup_key="key_a", ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(dedup_key="key_b", ts_ns=1_000_000_000_000_000_001, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None
