"""Tests for alert escalation chain."""
from __future__ import annotations

from hft_platform.notifications.alert import Alert, AlertSeverity


def _make_alert(
    *,
    alert_id: str = "a-001",
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    ts_ns: int = 1_000_000_000_000_000_000,
) -> Alert:
    return Alert(
        alert_id=alert_id, severity=severity, category="risk",
        source="storm_guard", title="Test alert", detail="Test detail",
        ts_ns=ts_ns, dedup_key=None, metadata=None,
    )


class TestEscalationTracker:
    def test_track_new_alert(self):
        from hft_platform.notifications.escalation import EscalationTracker
        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=3)
        alert = _make_alert()
        tracker.track(alert)
        assert tracker.is_tracked("a-001")

    def test_acknowledge_stops_tracking(self):
        from hft_platform.notifications.escalation import EscalationTracker
        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=2)
        tracker.track(_make_alert())
        assert tracker.is_tracked("a-001")
        tracker.acknowledge("a-001")
        assert not tracker.is_tracked("a-001")

    def test_due_escalations_returns_alerts_after_interval(self):
        from hft_platform.notifications.escalation import EscalationTracker
        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=3)
        # ts_ns at epoch; interval is 300 seconds (300_000_000_000 ns)
        # 100 seconds later → not yet due
        # 301 seconds later → due
        alert = _make_alert(ts_ns=1_000_000_000_000)
        tracker.track(alert)
        due = tracker.get_due(now_ns=1_100_000_000_000)   # +100 s — below threshold
        assert len(due) == 0
        due = tracker.get_due(now_ns=1_301_000_000_000)   # +301 s — above threshold
        assert len(due) == 1
        assert due[0].alert_id == "a-001"

    def test_max_escalations_reached(self):
        from hft_platform.notifications.escalation import EscalationTracker
        tracker = EscalationTracker(intervals_ns=[100_000_000_000], max_escalations=2)
        alert = _make_alert(ts_ns=1_000_000_000_000_000_000)
        tracker.track(alert)
        due = tracker.get_due(now_ns=1_101_000_000_000_000_000)
        assert len(due) == 1
        tracker.mark_escalated("a-001", now_ns=1_101_000_000_000_000_000)
        due = tracker.get_due(now_ns=1_202_000_000_000_000_000)
        assert len(due) == 1
        tracker.mark_escalated("a-001", now_ns=1_202_000_000_000_000_000)
        due = tracker.get_due(now_ns=1_303_000_000_000_000_000)
        assert len(due) == 0

    def test_info_alerts_not_tracked(self):
        from hft_platform.notifications.escalation import EscalationTracker
        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=3)
        alert = _make_alert(severity=AlertSeverity.INFO)
        tracker.track(alert)
        assert not tracker.is_tracked("a-001")

    def test_warn_alerts_not_tracked(self):
        from hft_platform.notifications.escalation import EscalationTracker
        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=3)
        alert = _make_alert(severity=AlertSeverity.WARN)
        tracker.track(alert)
        assert not tracker.is_tracked("a-001")
