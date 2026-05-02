"""Tests for HTML escaping in Telegram notification modules.

Verifies that external/exception-sourced strings are properly escaped
before being interpolated into Telegram HTML templates (R2-3, R2-4).
"""

from __future__ import annotations

from hft_platform.notifications.alertmanager_bridge import format_alert_message
from hft_platform.notifications.templates import (
    render_backup_failed,
    render_flatten_result,
    render_halt,
    render_position_recovery_failed,
    render_reconnect_alert,
    render_stormguard_change,
)

# ---------------------------------------------------------------------------
# R2-3: alertmanager_bridge
# ---------------------------------------------------------------------------


def test_alertmanager_escapes_angle_brackets() -> None:
    """Payload with <script> in summary must be escaped in output."""
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "TestAlert", "severity": "warning"},
                "annotations": {
                    "summary": "value > threshold <script>alert(1)</script>",
                    "description": "",
                },
            }
        ]
    }
    result = format_alert_message(payload)
    assert "&lt;script&gt;" in result
    assert "<script>" not in result
    assert "&gt;" in result


def test_alertmanager_escapes_name_and_severity() -> None:
    """Alert name and severity with angle brackets are escaped."""
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "Alert<X>", "severity": "crit<ical>"},
                "annotations": {"summary": "ok", "description": ""},
            }
        ]
    }
    result = format_alert_message(payload)
    assert "Alert&lt;X&gt;" in result
    assert "crit&lt;ical&gt;" in result
    # Intentional <b> tags must remain
    assert "<b>" in result
    assert "</b>" in result


def test_alertmanager_escapes_description() -> None:
    """Description field with HTML special chars is escaped."""
    payload = {
        "alerts": [
            {
                "status": "resolved",
                "labels": {"alertname": "A", "severity": "info"},
                "annotations": {
                    "summary": "normal",
                    "description": "disk usage > 90% & rising",
                },
            }
        ]
    }
    result = format_alert_message(payload)
    assert "&gt;" in result
    assert "&amp;" in result
    assert ">" not in result.split("</b>")[1]  # no bare > after bold tag


# ---------------------------------------------------------------------------
# R2-4: templates
# ---------------------------------------------------------------------------


def test_halt_reason_escapes_html() -> None:
    """render_halt escapes < and > in reason string."""
    reason = "price < 0 or price > 1000000"
    result = render_halt(reason)
    assert "&lt;" in result
    assert "&gt;" in result
    assert "<" not in result
    assert ">" not in result


def test_backup_failed_escapes_error() -> None:
    """render_backup_failed escapes angle brackets in error message."""
    error = "Connection <timeout>: host & port unreachable"
    result = render_backup_failed(date_str="2026-04-04", error=error, last_success_date="2026-04-03")
    assert "&lt;timeout&gt;" in result
    assert "&amp;" in result
    assert "<timeout>" not in result


def test_stormguard_change_escapes_reason() -> None:
    """render_stormguard_change escapes reason with HTML chars."""
    result = render_stormguard_change("NORMAL", "HALT", "drawdown > limit & position < 0")
    assert "&gt;" in result
    assert "&amp;" in result
    assert "&lt;" in result


def test_reconnect_alert_escapes_flap_status() -> None:
    """render_reconnect_alert escapes flap_status string."""
    result = render_reconnect_alert(3, "FLAPPING<high>")
    assert "&lt;high&gt;" in result
    assert "<high>" not in result


def test_position_recovery_failed_escapes_reason() -> None:
    """render_position_recovery_failed escapes reason string."""
    result = render_position_recovery_failed(
        source="dual",
        reason="broker qty > threshold & checkpoint < 0",
        mismatches=[],
    )
    assert "&gt;" in result
    assert "&amp;" in result
    assert "&lt;" in result


def test_flatten_result_escapes_failed_symbols() -> None:
    """render_flatten_result escapes symbols with HTML special chars (defensive)."""
    result = render_flatten_result(
        scope="all",
        fully_closed=0,
        partially_closed=0,
        failed=2,
        failed_symbols=["SYM<A>", "SYM&B"],
    )
    assert "&lt;A&gt;" in result
    assert "&amp;B" in result
    assert "<A>" not in result


def test_safe_html_tags_preserved() -> None:
    """<b> tags in alertmanager output are NOT escaped — they are template structure."""
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "NormalAlert", "severity": "critical"},
                "annotations": {"summary": "normal summary", "description": ""},
            }
        ]
    }
    result = format_alert_message(payload)
    assert "<b>" in result
    assert "</b>" in result
