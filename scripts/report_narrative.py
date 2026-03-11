#!/usr/bin/env python3
"""Shared report narrative engine for soak/daily operational reports.

Provides utility functions for generating actionable operational intelligence
from soak and daily report check data. Imported by soak_acceptance.py,
callback_latency_guard.py, and reliability_review_pack.py.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

_CATEGORY_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("service_", "restart_"), "Infrastructure"),
    (("execution_",), "Execution"),
    (("feed_", "quote_"), "Market Data Feed"),
    (("wal_", "recorder_"), "Persistence"),
    (("stormguard_",), "Risk Management"),
    (("raw_queue_",), "Queue Health"),
)

_DIAGNOSIS_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "restart_",
        "Service restarted during soak window; check docker logs for OOM, crash, or manual restart",
        "Infrastructure",
    ),
    (
        "feed_reconnect_failure_ratio_",
        "High reconnect failure rate; check Shioaji API status and network stability",
        "Market Data Feed",
    ),
    (
        "feed_session_conflict_",
        "Multiple runtimes competing for broker session; ensure only one hft-engine instance is running",
        "Market Data Feed",
    ),
    (
        "stormguard_halt_",
        "StormGuard entered HALT state; review risk thresholds and market conditions",
        "Risk Management",
    ),
    (
        "wal_backlog_",
        "WAL backlog elevated; check ClickHouse write performance and disk space",
        "Persistence",
    ),
    (
        "recorder_insert_failed_",
        "ClickHouse insert failures detected; check connection and schema compatibility",
        "Persistence",
    ),
    (
        "feed_first_quote_",
        "No first quote received; expected on trading days only (check if holiday/weekend)",
        "Market Data Feed",
    ),
    (
        "execution_gateway_",
        "Execution component degraded; check service health and restart history",
        "Execution",
    ),
    (
        "execution_router_",
        "Execution component degraded; check service health and restart history",
        "Execution",
    ),
)

_DEFAULT_DIAGNOSIS = "Check value exceeds threshold; investigate metric in Prometheus/Grafana"

_STATUS_RANK: dict[str, int] = {"pass": 0, "warn": 1, "fail": 2, "unknown": 1}

_STATUS_ICONS: dict[str, str] = {
    "pass": "\u2705",
    "warn": "\u26a0\ufe0f",
    "fail": "\u274c",
    "unknown": "\u2753",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_status_icon(status: str) -> str:
    """Return a unicode icon for the given check status."""
    return _STATUS_ICONS.get(status, "\u2753")


def sparkline(values: list[float], width: int = 7) -> str:
    """Return an ASCII sparkline string from *values*.

    Uses block element characters to represent relative magnitude.
    *width* controls the output length; values are down-sampled or
    right-padded as needed.
    """
    if not values:
        return _SPARK_CHARS[0] * width

    # Down-sample to *width* by picking evenly spaced indices.
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = list(values) + [0.0] * (width - len(values))

    lo = min(sampled)
    hi = max(sampled)
    span = hi - lo if hi != lo else 1.0
    last_idx = len(_SPARK_CHARS) - 1

    return "".join(_SPARK_CHARS[min(int((v - lo) / span * last_idx + 0.5), last_idx)] for v in sampled)


def compute_risk_score(checks: list[dict[str, Any]]) -> int:
    """Return a 0-100 risk score derived from check results.

    Scoring: fail+critical = +25, fail+warning = +10,
    warn+critical = +10, warn+warning = +3.
    """
    score = 0
    for c in checks:
        status = c.get("status", "unknown")
        severity = c.get("severity", "warning")
        if status == "fail":
            score += 25 if severity == "critical" else 10
        elif status == "warn":
            score += 10 if severity == "critical" else 3
    return min(score, 100)


def executive_summary(
    checks: list[dict[str, Any]],
    services: list[dict[str, Any]],
    overall_status: str,
    scope_date: str,
    expect_trading_day: bool,
) -> str:
    """Generate a 2-3 sentence health verdict from check results."""
    total = len(checks)
    pass_count = sum(1 for c in checks if c.get("status") == "pass")
    fail_count = sum(1 for c in checks if c.get("status") == "fail")
    warn_count = sum(1 for c in checks if c.get("status") == "warn")

    restarted = [s.get("service", "?") for s in services if (s.get("restart_count") or 0) > 0]

    parts: list[str] = []

    if fail_count > 0:
        fail_ids = [c["id"] for c in checks if c.get("status") == "fail"]
        summary = ", ".join(fail_ids[:3])
        if len(fail_ids) > 3:
            summary += f" (+{len(fail_ids) - 3} more)"
        parts.append(f"ALERT: {fail_count} critical check(s) failed — {summary}. Immediate investigation required.")
    else:
        parts.append(f"System is healthy with {pass_count}/{total} checks passing.")

    if restarted:
        svc_list = ", ".join(restarted)
        parts.append(f"{svc_list} restarted (warn).")

    if not expect_trading_day:
        parts.append(f"{scope_date} is a non-trading day; feed checks may show expected misses.")

    if warn_count > 0 and fail_count == 0:
        parts.append(f"{warn_count} warning(s) noted — no critical failures detected.")

    return " ".join(parts)


def group_checks_by_category(
    checks: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group checks into operational categories based on check_id prefix."""
    result: dict[str, list[dict[str, Any]]] = {}
    for c in checks:
        cid: str = c.get("id", "")
        category = "Other"
        for prefixes, cat in _CATEGORY_PREFIXES:
            if any(cid.startswith(p) for p in prefixes):
                category = cat
                break
        result.setdefault(category, []).append(c)
    return result


def diagnose_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rule-based root-cause diagnostics for non-pass checks."""
    results: list[dict[str, Any]] = []
    for c in checks:
        status = c.get("status", "unknown")
        if status == "pass":
            continue
        cid: str = c.get("id", "")
        severity = c.get("severity", "warning")

        diagnosis = _DEFAULT_DIAGNOSIS
        category = "Other"
        for prefix, diag, cat in _DIAGNOSIS_RULES:
            if cid.startswith(prefix):
                diagnosis = diag
                category = cat
                break
        else:
            # Fall back to category lookup for category field.
            for prefixes, cat in _CATEGORY_PREFIXES:
                if any(cid.startswith(p) for p in prefixes):
                    category = cat
                    break

        results.append(
            {
                "check_id": cid,
                "status": status,
                "severity": severity,
                "diagnosis": diagnosis,
                "category": category,
            }
        )
    return results


def recommend_actions(diagnosed_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return priority-sorted action items from diagnosed checks.

    Critical fails -> immediate, warning fails -> next_session, warns -> monitor.
    """
    actions: list[dict[str, Any]] = []
    for d in diagnosed_checks:
        status = d.get("status", "unknown")
        severity = d.get("severity", "warning")
        check_id = d.get("check_id", "?")
        diagnosis = d.get("diagnosis", "")

        if status == "fail" and severity == "critical":
            priority = 1
            urgency = "immediate"
        elif status == "fail":
            priority = 2
            urgency = "next_session"
        else:
            priority = 3
            urgency = "monitor"

        actions.append(
            {
                "priority": priority,
                "action": f"[{check_id}] {diagnosis}",
                "urgency": urgency,
            }
        )

    return sorted(actions, key=lambda a: a["priority"])


def trend_delta(
    current_checks: list[dict[str, Any]],
    previous_report_path: str | Path,
) -> list[dict[str, Any]]:
    """Compare current checks against a previous report JSON and return changes.

    Returns only checks whose status changed between reports.
    """
    prev_path = Path(previous_report_path)
    if not prev_path.exists():
        return []

    try:
        prev_report = json.loads(prev_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    prev_checks: list[dict[str, Any]] = prev_report.get("checks", [])
    prev_map: dict[str, str] = {c.get("id", ""): c.get("status", "unknown") for c in prev_checks}

    changes: list[dict[str, Any]] = []
    for c in current_checks:
        cid = c.get("id", "")
        curr_status = c.get("status", "unknown")
        prev_status = prev_map.get(cid)
        if prev_status is None or prev_status == curr_status:
            continue

        curr_rank = _STATUS_RANK.get(curr_status, 1)
        prev_rank = _STATUS_RANK.get(prev_status, 1)

        if curr_rank < prev_rank:
            direction = "improved"
        elif curr_rank > prev_rank:
            direction = "degraded"
        else:
            direction = "unchanged"

        changes.append(
            {
                "check_id": cid,
                "prev_status": prev_status,
                "curr_status": curr_status,
                "direction": direction,
            }
        )

    return changes


def render_trend_section(
    daily_dir: str | Path,
    current_date: str,
    lookback_days: int = 7,
) -> str:
    """Render a markdown trend section from the last *lookback_days* daily reports.

    Loads JSON reports from *daily_dir*, produces a table with per-day status,
    a risk-score sparkline, and degradation warnings.
    """
    ddir = Path(daily_dir)
    try:
        cur = dt.date.fromisoformat(current_date)
    except ValueError:
        return "_Invalid date for trend section._\n"

    # Collect reports within the lookback window.
    entries: list[tuple[dt.date, dict[str, Any]]] = []
    for i in range(lookback_days):
        d = cur - dt.timedelta(days=lookback_days - 1 - i)
        p = ddir / f"{d.isoformat()}.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries.append((d, data))

    if not entries:
        return "_No daily reports found for trend analysis._\n"

    # Build table rows.
    lines: list[str] = ["### Trend (last %d days)\n" % lookback_days]
    lines.append("| Date | Status | Risk |")
    lines.append("|------|--------|------|")

    risk_scores: list[float] = []
    for d, data in entries:
        overall = data.get("summary", {}).get("overall", "unknown")
        checks_list: list[dict[str, Any]] = data.get("checks", [])
        rs = compute_risk_score(checks_list)
        risk_scores.append(float(rs))
        icon = format_status_icon(overall)
        lines.append(f"| {d.isoformat()} | {icon} {overall} | {rs} |")

    lines.append("")
    lines.append(f"Risk sparkline: `{sparkline(risk_scores, width=min(len(risk_scores), 7))}`")
    lines.append("")

    # Degradation warnings: compare last two entries.
    if len(entries) >= 2:
        prev_checks = entries[-2][1].get("checks", [])
        curr_checks = entries[-1][1].get("checks", [])
        prev_map = {c.get("id", ""): c.get("status", "unknown") for c in prev_checks}

        degraded: list[str] = []
        for c in curr_checks:
            cid = c.get("id", "")
            cs = c.get("status", "unknown")
            ps = prev_map.get(cid)
            if ps == "pass" and cs in ("warn", "fail"):
                degraded.append(f"- `{cid}`: {ps} -> {cs}")

        if degraded:
            lines.append("**Degradations (vs previous day):**\n")
            lines.extend(degraded)
            lines.append("")

    return "\n".join(lines) + "\n"
