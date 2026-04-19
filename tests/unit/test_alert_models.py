"""Tests for alert data models."""

from __future__ import annotations

import pytest


def test_alert_severity_ordering():
    from hft_platform.notifications.alert import AlertSeverity

    assert AlertSeverity.INFO < AlertSeverity.WARN
    assert AlertSeverity.WARN < AlertSeverity.CRITICAL
    assert AlertSeverity.CRITICAL < AlertSeverity.FATAL


def test_alert_creation():
    from hft_platform.notifications.alert import Alert, AlertSeverity

    alert = Alert(
        alert_id="test-001",
        severity=AlertSeverity.WARN,
        category="feed",
        source="shioaji_client",
        title="Feed gap detected",
        detail="No ticks for 2.5 seconds on TMFD6",
        ts_ns=1_700_000_000_000_000_000,
        dedup_key="feed_gap:TMFD6",
        metadata={"symbol": "TMFD6", "gap_s": 2.5},
    )
    assert alert.severity == AlertSeverity.WARN
    assert alert.category == "feed"
    assert alert.dedup_key == "feed_gap:TMFD6"


def test_alert_is_frozen():
    from hft_platform.notifications.alert import Alert, AlertSeverity

    alert = Alert(
        alert_id="test-002",
        severity=AlertSeverity.INFO,
        category="ops",
        source="session_governor",
        title="Phase change",
        detail="futures_day: INIT -> OPEN",
        ts_ns=1_700_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    with pytest.raises(AttributeError):
        alert.severity = AlertSeverity.FATAL


def test_silence_rule_matches_category():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-001",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=1_000_000_000_000_000_000,
        end_ns=2_000_000_000_000_000_000,
        reason="maintenance",
    )
    alert = Alert(
        alert_id="a-001",
        severity=AlertSeverity.WARN,
        category="feed",
        source="shioaji_client",
        title="Test",
        detail="Test",
        ts_ns=1_500_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    assert rule.matches(alert)


def test_silence_rule_does_not_match_higher_severity():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-002",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=1_000_000_000_000_000_000,
        end_ns=2_000_000_000_000_000_000,
        reason="maintenance",
    )
    alert = Alert(
        alert_id="a-002",
        severity=AlertSeverity.CRITICAL,
        category="feed",
        source="shioaji_client",
        title="Test",
        detail="Test",
        ts_ns=1_500_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    assert not rule.matches(alert)


def test_silence_rule_does_not_match_outside_window():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-003",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=1_000_000_000_000_000_000,
        end_ns=2_000_000_000_000_000_000,
        reason="maintenance",
    )
    alert = Alert(
        alert_id="a-003",
        severity=AlertSeverity.INFO,
        category="feed",
        source="shioaji_client",
        title="Test",
        detail="Test",
        ts_ns=3_000_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    assert not rule.matches(alert)


def test_silence_rule_permanent_window():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-004",
        category=None,
        source=None,
        severity_max=AlertSeverity.INFO,
        start_ns=1_000_000_000_000_000_000,
        end_ns=0,
        reason="suppress info noise",
    )
    alert = Alert(
        alert_id="a-004",
        severity=AlertSeverity.INFO,
        category="broker",
        source="fubon",
        title="Test",
        detail="Test",
        ts_ns=9_000_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    assert rule.matches(alert)
