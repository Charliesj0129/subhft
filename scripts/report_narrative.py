#!/usr/bin/env python3
"""Report narrative engine — human-readable quality overlays for soak reports.

Provides executive summaries, risk scoring, trend analysis, diagnostics,
and actionable recommendations for daily/weekly soak acceptance reports.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

_SPARKLINE_CHARS = "▁▂▃▄▅▆▇█"

_STATUS_ICONS: dict[str, str] = {
    STATUS_PASS: "\u2705",      # checkmark
    STATUS_WARN: "\u26a0\ufe0f",  # warning
    STATUS_FAIL: "\u274c",      # cross
    STATUS_UNKNOWN: "\u2753",   # question mark
}

# Weight map for risk score computation (check_id prefix -> weight per failure).
_RISK_WEIGHTS: dict[str, int] = {
    "service_": 10,
    "feed_": 15,
    "restart_": 5,
    "session_": 20,
    "stormguard_": 25,
    "wal_": 10,
    "docker_": 10,
}

# Category grouping by check_id prefix.
_CATEGORY_MAP: dict[str, str] = {
    "service_": "Infrastructure",
    "feed_": "Market Data Feed",
    "restart_": "Service Restarts",
    "session_": "Session Management",
    "stormguard_": "Risk / StormGuard",
    "wal_": "Persistence / WAL",
    "docker_": "Docker / Containers",
}

# Diagnosis templates keyed by check_id substring.
_DIAGNOSIS_TEMPLATES: dict[str, dict[str, str]] = {
    "restart": {
        "cause": "Service restarted unexpectedly during soak window",
        "impact": "Possible data gap or state loss during restart",
        "suggestion": "Check container logs around restart timestamp",
    },
    "feed_reconnect": {
        "cause": "Market data feed reconnection detected",
        "impact": "Tick data gap during reconnect; strategies may miss signals",
        "suggestion": "Review feed adapter reconnect logic and network stability",
    },
    "session": {
        "cause": "Broker session conflict or expiry detected",
        "impact": "Order placement may have been blocked during session issue",
        "suggestion": "Review session refresh thread and lease renewal",
    },
    "stormguard": {
        "cause": "StormGuard risk circuit breaker triggered",
        "impact": "Trading was halted by risk guard; potential missed alpha",
        "suggestion": "Check StormGuard FSM transitions and trigger thresholds",
    },
    "wal": {
        "cause": "WAL backlog exceeded acceptable threshold",
        "impact": "Persistence lag may cause data loss on crash",
        "suggestion": "Check ClickHouse write throughput and WAL flush interval",
    },
}

_DEFAULT_DIAGNOSIS: dict[str, str] = {
    "cause": "Check failed or degraded beyond threshold",
    "impact": "System health may be compromised",
    "suggestion": "Investigate check details and recent changes",
}

# Priority map for recommendation urgency (lower = more urgent).
_SEVERITY_PRIORITY: dict[str, int] = {
    "critical": 1,
    "warning": 2,
    "info": 3,
}


def executive_summary(
    checks: list[dict],
    services: list[dict],
    overall_status: str,
    scope_date: str = "",
    expect_trading_day: bool = True,
) -> str:
    """Return a human-readable executive summary paragraph."""
    n_checks = len(checks)
    n_pass = sum(1 for c in checks if c.get("status") == STATUS_PASS)
    n_warn = sum(1 for c in checks if c.get("status") == STATUS_WARN)
    n_fail = sum(1 for c in checks if c.get("status") == STATUS_FAIL)
    n_services = len(services)
    n_healthy = sum(1 for s in services if s.get("health") == "healthy")

    date_ctx = f" for {scope_date}" if scope_date else ""

    if not expect_trading_day:
        return (
            f"Non-trading day report{date_ctx}. "
            f"{n_checks} checks evaluated ({n_pass} pass, {n_warn} warn, {n_fail} fail). "
            f"{n_services} services monitored ({n_healthy} healthy). "
            "Reduced activity expected."
        )

    if overall_status == STATUS_PASS:
        return (
            f"System healthy{date_ctx}. "
            f"All {n_checks} checks passed. "
            f"{n_services} services monitored ({n_healthy} healthy). "
            "No action required."
        )
    elif overall_status == STATUS_WARN:
        return (
            f"System operational with warnings{date_ctx}. "
            f"{n_warn} of {n_checks} checks raised warnings. "
            f"{n_services} services monitored ({n_healthy} healthy). "
            "Review warnings below."
        )
    else:
        return (
            f"ALERT: System degraded{date_ctx}. "
            f"{n_fail} of {n_checks} checks failed. "
            f"{n_services} services monitored ({n_healthy} healthy). "
            "Immediate attention required."
        )


def compute_risk_score(checks: list[dict]) -> int:
    """Compute a 0-100 risk score from check results.

    Each failing/warning check contributes weight based on its category.
    Critical severity doubles the weight.
    """
    if not checks:
        return 0

    score = 0
    for c in checks:
        status = c.get("status", STATUS_UNKNOWN)
        if status == STATUS_PASS:
            continue

        check_id = str(c.get("id", ""))
        severity = str(c.get("severity", "warning"))

        weight = 5  # default
        for prefix, w in _RISK_WEIGHTS.items():
            if check_id.startswith(prefix):
                weight = w
                break

        if status == STATUS_FAIL:
            contribution = weight
        elif status == STATUS_WARN:
            contribution = weight // 2
        else:
            contribution = weight // 3

        if severity == "critical":
            contribution *= 2

        score += contribution

    return min(score, 100)


def trend_delta(
    current_checks: list[dict],
    previous_report_path: str | Path | None,
) -> list[dict]:
    """Compare current checks against a previous report JSON file.

    Returns a list of dicts with keys: check_id, previous, current, direction.
    Only includes checks whose status changed.
    """
    if previous_report_path is None:
        return []

    prev_path = Path(previous_report_path)
    if not prev_path.exists():
        return []

    try:
        prev_data = json.loads(prev_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    prev_checks = {
        str(c.get("id")): str(c.get("status", STATUS_UNKNOWN))
        for c in prev_data.get("checks", [])
    }

    _order = {STATUS_PASS: 0, STATUS_UNKNOWN: 1, STATUS_WARN: 2, STATUS_FAIL: 3}
    deltas: list[dict] = []
    for c in current_checks:
        cid = str(c.get("id", ""))
        cur_status = str(c.get("status", STATUS_UNKNOWN))
        prev_status = prev_checks.get(cid)
        if prev_status is None or prev_status == cur_status:
            continue
        if _order.get(cur_status, 1) > _order.get(prev_status, 1):
            direction = "degraded"
        else:
            direction = "improved"
        deltas.append(
            {
                "check_id": cid,
                "previous": prev_status,
                "current": cur_status,
                "direction": direction,
            }
        )

    return deltas


def sparkline(values: list[float], width: int = 7) -> str:
    """Render a unicode sparkline from a list of numeric values."""
    if not values:
        return ""

    if len(values) == 1:
        return _SPARKLINE_CHARS[4]  # middle bar for single value

    mn = min(values)
    mx = max(values)
    rng = mx - mn

    chars: list[str] = []
    for v in values[-width:]:
        if rng == 0:
            idx = 0
        else:
            idx = int((v - mn) / rng * (len(_SPARKLINE_CHARS) - 1))
            idx = max(0, min(idx, len(_SPARKLINE_CHARS) - 1))
        chars.append(_SPARKLINE_CHARS[idx])

    return "".join(chars)


def diagnose_checks(checks: list[dict]) -> list[dict]:
    """Produce diagnostic details for non-passing checks.

    Returns a list of dicts with keys: check_id, status, severity, cause,
    impact, suggestion.
    """
    diagnosed: list[dict] = []
    for c in checks:
        status = c.get("status", STATUS_UNKNOWN)
        if status == STATUS_PASS:
            continue

        check_id = str(c.get("id", ""))
        template = _DEFAULT_DIAGNOSIS
        for key, tmpl in _DIAGNOSIS_TEMPLATES.items():
            if key in check_id:
                template = tmpl
                break

        diagnosed.append(
            {
                "check_id": check_id,
                "status": status,
                "severity": c.get("severity", "warning"),
                "cause": template["cause"],
                "impact": template["impact"],
                "suggestion": template["suggestion"],
            }
        )

    return diagnosed


def recommend_actions(diagnosed_checks: list[dict]) -> list[dict]:
    """Produce prioritized action recommendations from diagnosed checks.

    Returns a list of dicts with keys: priority, check_id, urgency, action.
    Sorted by priority (lower number = more urgent).
    """
    if not diagnosed_checks:
        return []

    actions: list[dict] = []
    for d in diagnosed_checks:
        severity = str(d.get("severity", "warning"))
        priority = _SEVERITY_PRIORITY.get(severity, 3)

        if severity == "critical" and d.get("status") == STATUS_FAIL:
            urgency = "immediate"
        elif d.get("status") == STATUS_FAIL:
            urgency = "high"
        elif d.get("status") == STATUS_WARN:
            urgency = "medium"
        else:
            urgency = "low"

        actions.append(
            {
                "priority": priority,
                "check_id": d.get("check_id", ""),
                "urgency": urgency,
                "action": d.get("suggestion", "Investigate"),
            }
        )

    actions.sort(key=lambda x: (x["priority"], x["check_id"]))
    return actions


def format_status_icon(status: str) -> str:
    """Return a unicode icon for a given status string."""
    return _STATUS_ICONS.get(status, _STATUS_ICONS[STATUS_UNKNOWN])


def group_checks_by_category(checks: list[dict]) -> dict[str, list]:
    """Group checks into categories by check_id prefix.

    Returns a dict mapping category name to list of checks.
    """
    groups: dict[str, list] = {}
    for c in checks:
        check_id = str(c.get("id", ""))
        category = "Other"
        for prefix, cat in _CATEGORY_MAP.items():
            if check_id.startswith(prefix):
                category = cat
                break
        groups.setdefault(category, []).append(c)
    return groups


def render_trend_section(
    daily_dir: str | Path,
    current_date: str,
    lookback_days: int = 7,
) -> str:
    """Render a markdown trend section from historical daily JSON reports.

    Scans daily_dir for JSON reports, builds a table and sparkline for
    the most recent lookback_days.
    """
    daily_path = Path(daily_dir)
    if not daily_path.exists():
        return "No historical data available for trend analysis."

    try:
        cur_date = dt.date.fromisoformat(current_date)
    except ValueError:
        return "Invalid date format for trend analysis."

    reports: list[tuple[dt.date, dict]] = []
    for p in sorted(daily_path.glob("*.json")):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d > cur_date:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            reports.append((d, data))
        except (json.JSONDecodeError, OSError):
            continue

    reports = reports[-lookback_days:]
    if not reports:
        return "No historical data available for trend analysis."

    # Build pass-rate series for sparkline
    pass_rates: list[float] = []
    for _d, data in reports:
        counts = data.get("summary", {}).get("counts", {})
        total = sum(counts.get(s, 0) for s in [STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_UNKNOWN])
        if total > 0:
            pass_rates.append(counts.get(STATUS_PASS, 0) / total)
        else:
            pass_rates.append(0.0)

    spark = sparkline(pass_rates, width=lookback_days)

    lines: list[str] = []
    lines.append("### Trend")
    lines.append("")
    lines.append(f"Pass rate sparkline (last {len(reports)} days): `{spark}`")
    lines.append("")
    lines.append("| Date | Overall | Pass | Warn | Fail |")
    lines.append("|------|---------|------|------|------|")
    for d, data in reports:
        overall = data.get("summary", {}).get("overall", STATUS_UNKNOWN)
        counts = data.get("summary", {}).get("counts", {})
        icon = format_status_icon(overall)
        lines.append(
            f"| {d.isoformat()} | {icon} `{overall}` "
            f"| {counts.get(STATUS_PASS, 0)} "
            f"| {counts.get(STATUS_WARN, 0)} "
            f"| {counts.get(STATUS_FAIL, 0)} |"
        )

    return "\n".join(lines)
