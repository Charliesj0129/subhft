"""Regression tests for config/monitoring/alerts/alertmanager.yml off-hours mute.

These tests ensure the AM mute_time_intervals stays in sync with the
TAIFEX trading-hours definition in core/market_calendar.py (08:45-13:45 day +
15:00-05:00 night, Asia/Taipei, weekdays). They are structural — they validate
that the config declares the expected boundaries; AM's own time-window logic
is validated separately by amtool check-config.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

AM_PATH = Path(__file__).resolve().parents[2] / "config" / "monitoring" / "alerts" / "alertmanager.yml"
RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "monitoring" / "alerts" / "rules.yaml"


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


_MATCHER = re.compile(r'^\s*(?P<label>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>=~|!~|!=|=)\s*"?(?P<value>.*?)"?\s*$')


def _route_matches(route: dict, labels: dict[str, str]) -> bool:
    """Alertmanager matching for the `match` and `matchers` forms (regexes are fully anchored)."""
    for key, value in (route.get("match") or {}).items():
        if labels.get(key) != value:
            return False
    for text in route.get("matchers") or []:
        m = _MATCHER.match(text)
        assert m, f"unparseable matcher {text!r}"
        actual = labels.get(m["label"], "")
        op, value = m["op"], m["value"]
        ok = {
            "=": actual == value,
            "!=": actual != value,
            "=~": re.fullmatch(value, actual) is not None,
            "!~": re.fullmatch(value, actual) is None,
        }[op]
        if not ok:
            return False
    return True


def _first_route(alertname: str, severity: str) -> tuple[int, dict]:
    for idx, route in enumerate(_load_am()["route"]["routes"]):
        if _route_matches(route, {"alertname": alertname, "severity": severity}):
            return idx, route
    raise AssertionError(f"no route matches {alertname}/{severity}")


def _rule_names() -> set[str]:
    doc = yaml.safe_load(RULES_PATH.read_text())
    return {r["alert"] for g in doc["groups"] for r in g["rules"] if "alert" in r}


INFRA_ALERTS = (
    "HFTEngineDown",
    "ScrapeTargetDown",
    "BackupFailed",
    "BackupStale",
    "BackupNotRunning",
    "HostDiskSpaceCritical",
)
LATCHED_ALERTS = ("StormGuardHalt", "KillSwitchActive", "KillSwitchFlattenBlocked")


def test_infra_alerts_route_first_and_are_not_muted_off_hours():
    """The 14:30 Taipei backup cron sits inside the mute; an engine/disk/backup page must not wait for 15:00."""
    routes = _load_am()["route"]["routes"]
    names = _rule_names()
    for alertname in INFRA_ALERTS:
        assert alertname in names, f"{alertname} is routed here but rules.yaml does not define it"
        idx, route = _first_route(alertname, "critical" if "Warn" not in alertname else "warning")
        assert idx == 0, f"{alertname} must hit the first route, got index {idx}"
        assert "mute_time_intervals" not in route, f"{alertname} must not be muted off-hours: {route!r}"
        assert route["receiver"] == "telegram-bridge"
        assert route["repeat_interval"] == "1h"
        assert route["continue"] is False
    assert routes[0]["group_wait"] == "10s" and routes[0]["group_interval"] == "1m"


def test_infra_route_does_not_swallow_trading_alerts():
    for alertname in ("StormGuardHalt", "OrderOutcomeUnresolved", "BrokerPathMostlySilent"):
        idx, _ = _first_route(alertname, "critical")
        assert idx != 0, f"{alertname} hit the unmuted infra route"


def test_disk_space_warning_stays_muted_off_hours():
    """Only the critical disk alert is urgent enough to page outside trading hours."""
    idx, route = _first_route("HostDiskSpaceWarn", "warning")
    assert idx != 0, "HostDiskSpaceWarn hit the unmuted infra route"
    assert route.get("mute_time_intervals") == ["taipei_off_hours"]


def test_latched_state_alerts_repeat_every_4h_and_stay_muted_off_hours():
    names = _rule_names()
    for alertname in LATCHED_ALERTS:
        assert alertname in names, f"{alertname} is routed here but rules.yaml does not define it"
        idx, route = _first_route(alertname, "critical")
        assert idx == 1, f"{alertname} must hit the latched-state route right after the infra route, got {idx}"
        assert route["repeat_interval"] == "4h"
        assert route["mute_time_intervals"] == ["taipei_off_hours"]
        assert route["continue"] is False


def test_generic_critical_route_still_repeats_every_15m_and_is_muted_after_the_specific_routes():
    cfg = _load_am()
    idx, route = _first_route("OrderOutcomeUnresolved", "critical")
    assert idx == 2, f"generic critical must follow the infra and latched routes, got index {idx}"
    assert route["match"] == {"severity": "critical"}
    assert route["repeat_interval"] == "15m"
    assert route["mute_time_intervals"] == ["taipei_off_hours"]
    # StormGuardHaltStuck is not latched-listed: it keeps the generic critical route.
    assert _first_route("StormGuardHaltStuck", "critical")[0] == idx
    assert "mute_time_intervals" not in cfg["route"]
