"""Regression tests for config/monitoring/alerts/alertmanager.yml off-hours mute.

These tests ensure the AM mute_time_intervals stays in sync with the
TAIFEX trading-hours definition in core/market_calendar.py (08:45-13:45 day +
15:00-05:00 night, Asia/Taipei, weekdays). They are structural — they validate
that the config declares the expected boundaries; AM's own time-window logic
is validated separately by amtool check-config.
"""

from __future__ import annotations

from pathlib import Path

import yaml

AM_PATH = Path(__file__).resolve().parents[2] / "config" / "monitoring" / "alerts" / "alertmanager.yml"


def _load_am() -> dict:
    with AM_PATH.open() as f:
        return yaml.safe_load(f)


def test_taipei_off_hours_interval_declared():
    """A `taipei_off_hours` time_interval must exist at the top level."""
    cfg = _load_am()
    intervals = {item["name"]: item for item in cfg.get("time_intervals", [])}
    assert "taipei_off_hours" in intervals, (
        "Missing top-level `time_intervals: taipei_off_hours`. AM mute won't "
        "find the named interval and routes referencing it will fail to load."
    )


def test_taipei_off_hours_uses_asia_taipei_tz():
    """Every sub-interval must specify location=Asia/Taipei."""
    cfg = _load_am()
    intervals = {item["name"]: item for item in cfg.get("time_intervals", [])}
    sub = intervals["taipei_off_hours"]["time_intervals"]
    for entry in sub:
        assert entry.get("location") == "Asia/Taipei", (
            "All taipei_off_hours sub-intervals must be Asia/Taipei to align "
            f"with the engine's market_calendar TZ. Got: {entry!r}"
        )


def test_taipei_off_hours_covers_weekday_lunch_break():
    """Mon-Fri 13:45-15:00 (between day-close and night-open) must be muted."""
    cfg = _load_am()
    sub = cfg["time_intervals"][0]["time_intervals"]
    weekday_lunch = [
        e
        for e in sub
        if e.get("weekdays") == ["monday:friday"]
        and any(t.get("start_time") == "13:45" and t.get("end_time") == "15:00" for t in e.get("times", []))
    ]
    assert weekday_lunch, f"Off-hours mute must include weekday 13:45-15:00 lunch break. Sub-intervals: {sub!r}"


def test_taipei_off_hours_covers_weekday_morning_gap():
    """Mon-Fri 05:00-08:45 (between night-close and day-open) must be muted."""
    cfg = _load_am()
    sub = cfg["time_intervals"][0]["time_intervals"]
    weekday_morning = [
        e
        for e in sub
        if e.get("weekdays") == ["monday:friday"]
        and any(t.get("start_time") == "05:00" and t.get("end_time") == "08:45" for t in e.get("times", []))
    ]
    assert weekday_morning, f"Off-hours mute must include weekday 05:00-08:45 morning gap. Sub-intervals: {sub!r}"


def test_severity_routes_reference_off_hours_mute():
    """critical + warning sub-routes must reference taipei_off_hours."""
    cfg = _load_am()
    sub_routes = cfg["route"]["routes"]
    by_severity: dict[str, dict] = {}
    for r in sub_routes:
        sev = (r.get("match") or {}).get("severity")
        if sev:
            by_severity[sev] = r
    for sev in ("critical", "warning"):
        assert sev in by_severity, f"Missing severity={sev!r} sub-route"
        mutes = by_severity[sev].get("mute_time_intervals", [])
        assert "taipei_off_hours" in mutes, (
            f"severity={sev!r} sub-route must reference taipei_off_hours. Current mutes: {mutes!r}"
        )


def test_root_route_has_no_mute_time_intervals():
    """AM rejects mute_time_intervals at the root route — guard against regressions."""
    cfg = _load_am()
    assert "mute_time_intervals" not in cfg["route"], (
        "Root route must not declare mute_time_intervals (AM forbids it). Move the field down to each child route."
    )
