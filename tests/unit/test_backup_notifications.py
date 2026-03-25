"""Tests for backup notification templates and dispatcher methods."""

from __future__ import annotations


def test_render_backup_success_contains_key_fields():
    from hft_platform.notifications.templates import render_backup_success

    msg = render_backup_success(
        date_str="2026-03-25",
        size_mb=1234.5,
        duration_s=42.3,
        retained_count=15,
    )
    assert "2026-03-25" in msg
    assert "1,234.5" in msg or "1234.5" in msg
    assert "42.3" in msg
    assert "15" in msg


def test_render_backup_failed_contains_error_and_last_success():
    from hft_platform.notifications.templates import render_backup_failed

    msg = render_backup_failed(
        date_str="2026-03-25",
        error="Disk full",
        last_success_date="2026-03-24",
    )
    assert "2026-03-25" in msg
    assert "Disk full" in msg
    assert "2026-03-24" in msg
    assert "FAIL" in msg.upper() or "失敗" in msg
