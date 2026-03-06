#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().astimezone().isoformat()


def _stamp() -> str:
    return _now_utc().strftime("%Y%m%dT%H%M%SZ")


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    if order.get(incoming, 0) > order.get(current, 0):
        return incoming
    return current


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
    checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
    sections = report.get("sections", {}) if isinstance(report.get("sections"), dict) else {}

    lines: list[str] = []
    lines.append("# Monthly Reliability Review Pack")
    lines.append("")
    lines.append(f"- generated_at: `{report.get('generated_at')}`")
    lines.append(f"- month: `{report.get('month')}`")
    lines.append(f"- overall: `{result.get('overall')}`")
    lines.append("")

    lines.append("## Gate Checks")
    lines.append("")
    lines.append("| id | status | severity | message |")
    lines.append("|---|---|---|---|")
    for check in checks:
        if not isinstance(check, dict):
            continue
        lines.append(
            f"| `{check.get('id')}` | `{check.get('status')}` | `{check.get('severity')}` | {check.get('message')} |"
        )
    lines.append("")

    for name in [
        "soak",
        "backlog",
        "drift",
        "disk",
        "drill",
        "release_channel",
        "query_guard",
        "feature_canary",
        "callback_latency",
    ]:
        payload = sections.get(name)
        lines.append(f"## {name}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(payload, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_month(raw: str | None) -> tuple[int, int]:
    if raw:
        value = raw.strip()
        if len(value) != 7 or value[4] != "-":
            raise ValueError("month must be YYYY-MM")
        y = int(value[0:4])
        m = int(value[5:7])
        if m < 1 or m > 12:
            raise ValueError("month must be YYYY-MM")
        return y, m

    now = dt.date.today()
    return now.year, now.month


def _month_range(year: int, month: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, month, 1)
    end_day = calendar.monthrange(year, month)[1]
    end = dt.date(year, month, end_day)
    return start, end


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _collect_daily_reports(soak_dir: Path, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    daily_dir = soak_dir / "daily"
    rows: list[dict[str, Any]] = []
    if not daily_dir.exists():
        return rows

    for p in sorted(daily_dir.glob("*.json")):
        try:
            day = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if day < start or day > end:
            continue
        obj = _read_json(p)
        if obj is None:
            continue
        rows.append({"day": day.isoformat(), "path": str(p.resolve()), "report": obj})
    return rows


def _collect_reports_by_generated_month(paths: list[Path], year: int, month: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(paths):
        obj = _read_json(p)
        if obj is None:
            continue
        generated = obj.get("generated_at")
        if not isinstance(generated, str):
            continue
        try:
            parsed = dt.datetime.fromisoformat(generated.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.year == year and parsed.month == month:
            out.append({"path": str(p.resolve()), "report": obj, "generated_at": generated})
    return out


def _latest_json(paths: list[Path]) -> dict[str, Any] | None:
    for p in sorted(paths, reverse=True):
        obj = _read_json(p)
        if obj is not None:
            return {"path": str(p.resolve()), "report": obj}
    return None


def _extract_check(report: dict[str, Any], check_id: str) -> dict[str, Any] | None:
    checks = report.get("checks")
    if not isinstance(checks, list):
        return None
    for row in checks:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") == check_id:
            return row
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    xs = sorted(values)
    idx = (len(xs) - 1) * q
    low = math.floor(idx)
    high = math.ceil(idx)
    if low == high:
        return xs[int(idx)]
    frac = idx - low
    return xs[low] * (1.0 - frac) + xs[high] * frac


def _summarize_soak(daily_reports: list[dict[str, Any]], soak_dir: Path, year: int, month: int) -> dict[str, Any]:
    counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for row in daily_reports:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        overall = str(summary.get("overall") or STATUS_UNKNOWN)
        if overall not in counts:
            overall = STATUS_UNKNOWN
        counts[overall] += 1

    latest_weekly = _latest_json(list((soak_dir / "weekly").glob("week_*.json")))
    latest_canary = _latest_json(list((soak_dir / "canary").glob("canary_*.json")))

    return {
        "daily_days": len(daily_reports),
        "daily_overall_counts": counts,
        "latest_weekly": latest_weekly,
        "latest_canary": latest_canary,
        "reports_in_month": {
            "weekly": len(
                _collect_reports_by_generated_month(list((soak_dir / "weekly").glob("week_*.json")), year, month)
            ),
            "canary": len(
                _collect_reports_by_generated_month(list((soak_dir / "canary").glob("canary_*.json")), year, month)
            ),
        },
    }


def _summarize_backlog(daily_reports: list[dict[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    by_day: list[dict[str, Any]] = []

    for row in daily_reports:
        day = str(row.get("day") or "")
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        check = _extract_check(report, "wal_backlog_max_24h")
        if not isinstance(check, dict):
            by_day.append({"day": day, "value": None, "status": STATUS_UNKNOWN})
            continue
        value = _as_float(check.get("value"))
        status = str(check.get("status") or STATUS_UNKNOWN)
        by_day.append({"day": day, "value": value, "status": status})
        if value is not None:
            values.append(value)

    avg = (sum(values) / len(values)) if values else None
    p95 = _percentile(values, 0.95)
    p99 = _percentile(values, 0.99)
    peak = max(values) if values else None

    return {
        "samples": len(values),
        "peak": peak,
        "avg": avg,
        "p95": p95,
        "p99": p99,
        "daily": by_day,
    }


def _summarize_drift(deploy_dir: Path, year: int, month: int) -> dict[str, Any]:
    checks = list((deploy_dir / "checks").glob("check_*.json"))
    in_month = _collect_reports_by_generated_month(checks, year, month)

    overall_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for row in in_month:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        overall = str(result.get("overall") or STATUS_UNKNOWN)
        if overall not in overall_counts:
            overall = STATUS_UNKNOWN
        overall_counts[overall] += 1

    latest = _latest_json(checks)
    latest_overall = None
    if latest and isinstance(latest.get("report"), dict):
        latest_overall = ((latest["report"].get("result") or {}).get("overall"))

    return {
        "checks_in_month": len(in_month),
        "overall_counts": overall_counts,
        "latest": latest,
        "latest_overall": latest_overall,
    }


def _summarize_release_channel(deploy_dir: Path, year: int, month: int) -> dict[str, Any]:
    decisions = list((deploy_dir / "release_channel" / "decisions").glob("release_gate_*.json"))
    promotions = list((deploy_dir / "release_channel" / "promotions").glob("stable_*.json"))

    decisions_in_month = _collect_reports_by_generated_month(decisions, year, month)
    promotions_in_month = _collect_reports_by_generated_month(promotions, year, month)

    decision_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for row in decisions_in_month:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        overall = str(result.get("overall") or STATUS_UNKNOWN)
        if overall not in decision_counts:
            overall = STATUS_UNKNOWN
        decision_counts[overall] += 1

    return {
        "decisions_in_month": len(decisions_in_month),
        "decision_overall_counts": decision_counts,
        "promotions_in_month": len(promotions_in_month),
        "latest_decision": _latest_json(decisions),
        "latest_promotion": _latest_json(promotions),
    }


def _summarize_query_guard(query_guard_dir: Path, year: int, month: int) -> dict[str, Any]:
    checks = list((query_guard_dir / "checks").glob("check_*.json"))
    runs = list((query_guard_dir / "runs").glob("run_*.json"))
    suites = list((query_guard_dir / "suites").glob("suite_*.json"))

    checks_in_month = _collect_reports_by_generated_month(checks, year, month)
    runs_in_month = _collect_reports_by_generated_month(runs, year, month)
    suites_in_month = _collect_reports_by_generated_month(suites, year, month)

    check_overall_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for row in checks_in_month:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        overall = str(result.get("overall") or STATUS_UNKNOWN)
        if overall not in check_overall_counts:
            overall = STATUS_UNKNOWN
        check_overall_counts[overall] += 1

    run_status_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    blocked_runs = 0
    for row in runs_in_month:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        execution = report.get("execution", {}) if isinstance(report.get("execution"), dict) else {}
        status = str(execution.get("status") or STATUS_UNKNOWN)
        if status not in run_status_counts:
            status = STATUS_UNKNOWN
        run_status_counts[status] += 1
        if execution.get("allowed") is False:
            blocked_runs += 1

    suite_overall_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for row in suites_in_month:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        overall = str(result.get("overall") or STATUS_UNKNOWN)
        if overall not in suite_overall_counts:
            overall = STATUS_UNKNOWN
        suite_overall_counts[overall] += 1

    return {
        "checks_in_month": len(checks_in_month),
        "check_overall_counts": check_overall_counts,
        "runs_in_month": len(runs_in_month),
        "run_status_counts": run_status_counts,
        "blocked_runs_in_month": blocked_runs,
        "suites_in_month": len(suites_in_month),
        "suite_overall_counts": suite_overall_counts,
        "latest_check": _latest_json(checks),
        "latest_run": _latest_json(runs),
        "latest_suite": _latest_json(suites),
    }


def _summarize_feature_canary(feature_canary_dir: Path, year: int, month: int) -> dict[str, Any]:
    reports = list(feature_canary_dir.glob("feature_canary_*.json"))
    in_month = _collect_reports_by_generated_month(reports, year, month)

    overall_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for row in in_month:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        overall = str(result.get("overall") or STATUS_UNKNOWN)
        if overall not in overall_counts:
            overall = STATUS_UNKNOWN
        overall_counts[overall] += 1

    latest = _latest_json(reports)
    latest_overall = None
    if latest and isinstance(latest.get("report"), dict):
        latest_overall = (
            (latest["report"].get("result") or {}).get("overall")
            if isinstance(latest["report"].get("result"), dict)
            else None
        )

    return {
        "reports_in_month": len(in_month),
        "overall_counts": overall_counts,
        "latest": latest,
        "latest_overall": latest_overall,
    }


def _summarize_callback_latency(callback_latency_dir: Path, year: int, month: int) -> dict[str, Any]:
    reports = list(callback_latency_dir.glob("callback_latency_*.json"))
    in_month = _collect_reports_by_generated_month(reports, year, month)

    overall_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for row in in_month:
        report = row.get("report", {}) if isinstance(row.get("report"), dict) else {}
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        overall = str(result.get("overall") or STATUS_UNKNOWN)
        if overall not in overall_counts:
            overall = STATUS_UNKNOWN
        overall_counts[overall] += 1

    latest = _latest_json(reports)
    latest_overall = None
    if latest and isinstance(latest.get("report"), dict):
        latest_overall = (
            (latest["report"].get("result") or {}).get("overall")
            if isinstance(latest["report"].get("result"), dict)
            else None
        )

    return {
        "reports_in_month": len(in_month),
        "overall_counts": overall_counts,
        "latest": latest,
        "latest_overall": latest_overall,
    }


def _run_cmd(command: list[str], cwd: Path | None = None, timeout_s: int = 180) -> tuple[int, str, str]:
    proc = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _snapshot_disk(paths: list[str], project_root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for raw in paths:
        val = raw.strip()
        if not val:
            continue
        p = Path(val)
        if not p.is_absolute():
            p = (project_root / val).resolve()

        if not p.exists():
            rows.append({"path": str(p), "status": STATUS_WARN, "error": "path_not_found"})
            continue

        rc, out, err = _run_cmd(["df", "-Pk", str(p)], cwd=project_root, timeout_s=10)
        if rc != 0:
            rows.append(
                {
                    "path": str(p),
                    "status": STATUS_FAIL,
                    "error": err.strip() or out.strip() or f"df_exit_{rc}",
                }
            )
            continue

        lines = [line for line in out.splitlines() if line.strip()]
        if len(lines) < 2:
            rows.append({"path": str(p), "status": STATUS_FAIL, "error": "df_parse_no_rows"})
            continue

        parts = lines[1].split()
        if len(parts) < 6:
            rows.append({"path": str(p), "status": STATUS_FAIL, "error": "df_parse_invalid_row"})
            continue

        try:
            blocks = int(parts[1])
            used = int(parts[2])
            available = int(parts[3])
            pct_text = parts[4].strip()
            used_pct = float(pct_text[:-1]) if pct_text.endswith("%") else None
        except ValueError:
            rows.append({"path": str(p), "status": STATUS_FAIL, "error": "df_parse_value_error"})
            continue

        rows.append(
            {
                "path": str(p),
                "status": STATUS_PASS,
                "filesystem": parts[0],
                "mount": parts[5],
                "blocks_kb": blocks,
                "used_kb": used,
                "available_kb": available,
                "available_gb": available / 1024 / 1024,
                "used_pct": used_pct,
            }
        )

    return {"paths": rows}


def _run_or_load_drill(
    *,
    output_dir: Path,
    project_root: Path,
    run_drill_suite: bool,
    drill_command: list[str],
) -> dict[str, Any]:
    drill_dir = output_dir / "drill_checks"
    drill_dir.mkdir(parents=True, exist_ok=True)

    if run_drill_suite:
        t0 = time.monotonic()
        rc, out, err = _run_cmd(drill_command, cwd=project_root, timeout_s=600)
        elapsed = time.monotonic() - t0
        artifact = {
            "generated_at": _now_iso(),
            "command": drill_command,
            "exit_code": rc,
            "duration_seconds": round(elapsed, 3),
            "status": STATUS_PASS if rc == 0 else STATUS_FAIL,
            "stdout_tail": "\n".join(out.splitlines()[-40:]),
            "stderr_tail": "\n".join(err.splitlines()[-40:]),
        }
        path = drill_dir / f"drill_{_stamp()}.json"
        _write_json(path, artifact)
        return {"latest": {"path": str(path.resolve()), "report": artifact}, "ran": True}

    latest = _latest_json(list(drill_dir.glob("drill_*.json")))
    return {"latest": latest, "ran": False}


def _evaluate(
    *,
    soak: dict[str, Any],
    backlog: dict[str, Any],
    drift: dict[str, Any],
    disk: dict[str, Any],
    drill: dict[str, Any],
    query_guard: dict[str, Any],
    feature_canary: dict[str, Any],
    callback_latency: dict[str, Any],
    min_disk_free_gb: float,
    backlog_p95_budget: float,
    backlog_p99_budget: float,
    min_query_guard_runs: int,
    min_query_guard_suite_runs: int,
    min_feature_canary_runs: int,
    min_callback_latency_runs: int,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(
        cid: str,
        ok: bool,
        *,
        severity: str,
        expected: Any,
        current: Any,
        message: str,
        allow_warn: bool = False,
    ) -> None:
        if ok:
            status = STATUS_PASS
        elif allow_warn:
            status = STATUS_WARN
        else:
            status = STATUS_FAIL
        checks.append(
            {
                "id": cid,
                "status": status,
                "severity": severity,
                "expected": expected,
                "current": current,
                "message": message,
            }
        )

    daily_days = int(soak.get("daily_days", 0))
    daily_counts = soak.get("daily_overall_counts", {}) if isinstance(soak.get("daily_overall_counts"), dict) else {}
    add(
        "soak_daily_reports_present",
        daily_days > 0,
        severity="critical",
        expected=">=1",
        current=daily_days,
        message="at least one daily soak report is required for monthly review",
    )

    add(
        "soak_no_fail_days",
        int(daily_counts.get(STATUS_FAIL, 0)) == 0,
        severity="critical",
        expected=0,
        current=daily_counts.get(STATUS_FAIL, 0),
        message="monthly soak window contains failing days",
        allow_warn=False,
    )

    latest_canary = soak.get("latest_canary") if isinstance(soak.get("latest_canary"), dict) else None
    canary_overall = None
    if latest_canary and isinstance(latest_canary.get("report"), dict):
        canary_overall = ((latest_canary["report"].get("result") or {}).get("overall"))

    add(
        "canary_report_present",
        latest_canary is not None,
        severity="critical",
        expected="latest canary report",
        current=latest_canary.get("path") if latest_canary else None,
        message="canary evidence is missing",
    )

    add(
        "canary_overall",
        canary_overall == STATUS_PASS,
        severity="critical",
        expected=STATUS_PASS,
        current=canary_overall,
        message="latest canary status is not pass",
        allow_warn=canary_overall == STATUS_WARN,
    )

    add(
        "backlog_p95_budget",
        (backlog.get("p95") is not None) and float(backlog.get("p95")) <= backlog_p95_budget,
        severity="warning",
        expected=f"<= {backlog_p95_budget}",
        current=backlog.get("p95"),
        message="wal backlog p95 exceeds budget",
        allow_warn=True,
    )

    add(
        "backlog_p99_budget",
        (backlog.get("p99") is not None) and float(backlog.get("p99")) <= backlog_p99_budget,
        severity="warning",
        expected=f"<= {backlog_p99_budget}",
        current=backlog.get("p99"),
        message="wal backlog p99 exceeds budget",
        allow_warn=True,
    )

    latest_drift = drift.get("latest") if isinstance(drift.get("latest"), dict) else None
    latest_drift_overall = drift.get("latest_overall")

    add(
        "drift_report_present",
        latest_drift is not None,
        severity="critical",
        expected="latest drift report",
        current=latest_drift.get("path") if latest_drift else None,
        message="drift evidence is missing",
    )

    add(
        "drift_overall",
        latest_drift_overall == STATUS_PASS,
        severity="critical",
        expected=STATUS_PASS,
        current=latest_drift_overall,
        message="latest drift status is not pass",
        allow_warn=latest_drift_overall == STATUS_WARN,
    )

    path_rows = disk.get("paths", []) if isinstance(disk.get("paths"), list) else []
    free_gb_values = [
        float(row.get("available_gb"))
        for row in path_rows
        if isinstance(row, dict) and _as_float(row.get("available_gb")) is not None
    ]
    min_free_gb = min(free_gb_values) if free_gb_values else None
    add(
        "disk_min_free_gb",
        (min_free_gb is not None) and min_free_gb >= min_disk_free_gb,
        severity="warning",
        expected=f">= {min_disk_free_gb}",
        current=None if min_free_gb is None else round(min_free_gb, 3),
        message="disk free space below monthly review threshold",
        allow_warn=True,
    )

    latest_drill = drill.get("latest") if isinstance(drill.get("latest"), dict) else None
    drill_status = None
    if latest_drill and isinstance(latest_drill.get("report"), dict):
        drill_status = latest_drill["report"].get("status")

    add(
        "drill_evidence_present",
        latest_drill is not None,
        severity="warning",
        expected="drill artifact json",
        current=latest_drill.get("path") if latest_drill else None,
        message="drill evidence missing (run with --run-drill-suite)",
        allow_warn=True,
    )

    add(
        "drill_status",
        drill_status == STATUS_PASS,
        severity="critical",
        expected=STATUS_PASS,
        current=drill_status,
        message="latest drill check is not pass",
        allow_warn=drill_status in {STATUS_UNKNOWN, None},
    )

    qg_runs = int(query_guard.get("runs_in_month", 0))
    qg_run_counts = (
        query_guard.get("run_status_counts", {})
        if isinstance(query_guard.get("run_status_counts"), dict)
        else {}
    )
    qg_fail_runs = int(qg_run_counts.get(STATUS_FAIL, 0))
    qg_checks = int(query_guard.get("checks_in_month", 0))
    qg_latest_check = query_guard.get("latest_check") if isinstance(query_guard.get("latest_check"), dict) else None
    qg_latest_run = query_guard.get("latest_run") if isinstance(query_guard.get("latest_run"), dict) else None
    qg_suites = int(query_guard.get("suites_in_month", 0))
    qg_suite_counts = (
        query_guard.get("suite_overall_counts", {})
        if isinstance(query_guard.get("suite_overall_counts"), dict)
        else {}
    )
    qg_fail_suites = int(qg_suite_counts.get(STATUS_FAIL, 0))
    qg_latest_suite = query_guard.get("latest_suite") if isinstance(query_guard.get("latest_suite"), dict) else None

    add(
        "query_guard_check_present",
        qg_checks > 0,
        severity="warning",
        expected=">=1 check artifact",
        current=qg_checks,
        message="no query-guard check artifacts in monthly window",
        allow_warn=True,
    )

    add(
        "query_guard_latest_run_present",
        qg_latest_run is not None,
        severity="warning",
        expected="latest run artifact exists",
        current=qg_latest_run.get("path") if qg_latest_run else None,
        message="query-guard run artifact is missing",
        allow_warn=True,
    )

    add(
        "query_guard_min_runs",
        qg_runs >= min_query_guard_runs,
        severity="warning",
        expected=f">= {min_query_guard_runs}",
        current=qg_runs,
        message="query-guard run count below policy threshold",
        allow_warn=True,
    )

    add(
        "query_guard_no_failed_runs",
        qg_fail_runs == 0,
        severity="warning",
        expected=0,
        current=qg_fail_runs,
        message="query-guard runs contain failed executions",
        allow_warn=True,
    )

    add(
        "query_guard_suite_present",
        qg_suites > 0,
        severity="warning",
        expected=">=1 suite artifact",
        current=qg_suites,
        message="no query-guard suite artifacts in monthly window",
        allow_warn=True,
    )

    add(
        "query_guard_min_suite_runs",
        qg_suites >= min_query_guard_suite_runs,
        severity="warning",
        expected=f">= {min_query_guard_suite_runs}",
        current=qg_suites,
        message="query-guard suite run count below policy threshold",
        allow_warn=True,
    )

    add(
        "query_guard_no_failed_suites",
        qg_fail_suites == 0,
        severity="warning",
        expected=0,
        current=qg_fail_suites,
        message="query-guard suite artifacts contain fail status",
        allow_warn=True,
    )

    if qg_latest_check and isinstance(qg_latest_check.get("report"), dict):
        latest_overall = (
            (qg_latest_check["report"].get("result") or {}).get("overall")
            if isinstance(qg_latest_check["report"].get("result"), dict)
            else None
        )
        add(
            "query_guard_latest_check_overall",
            latest_overall in {STATUS_PASS, STATUS_WARN},
            severity="warning",
            expected="pass|warn",
            current=latest_overall,
            message="latest query-guard check has fail status",
            allow_warn=True,
        )

    if qg_latest_suite and isinstance(qg_latest_suite.get("report"), dict):
        latest_suite_overall = (
            (qg_latest_suite["report"].get("result") or {}).get("overall")
            if isinstance(qg_latest_suite["report"].get("result"), dict)
            else None
        )
        add(
            "query_guard_latest_suite_overall",
            latest_suite_overall in {STATUS_PASS, STATUS_WARN},
            severity="warning",
            expected="pass|warn",
            current=latest_suite_overall,
            message="latest query-guard suite has fail status",
            allow_warn=True,
        )

    fc_reports = int(feature_canary.get("reports_in_month", 0))
    fc_overall_counts = (
        feature_canary.get("overall_counts", {})
        if isinstance(feature_canary.get("overall_counts"), dict)
        else {}
    )
    fc_fail_reports = int(fc_overall_counts.get(STATUS_FAIL, 0))
    fc_latest = feature_canary.get("latest") if isinstance(feature_canary.get("latest"), dict) else None
    fc_latest_overall = feature_canary.get("latest_overall")

    add(
        "feature_canary_latest_report_present",
        fc_latest is not None,
        severity="warning",
        expected="latest feature canary artifact exists",
        current=fc_latest.get("path") if fc_latest else None,
        message="feature canary artifact is missing",
        allow_warn=True,
    )

    add(
        "feature_canary_min_runs",
        fc_reports >= min_feature_canary_runs,
        severity="warning",
        expected=f">= {min_feature_canary_runs}",
        current=fc_reports,
        message="feature canary run count below policy threshold",
        allow_warn=True,
    )

    add(
        "feature_canary_no_fail_reports",
        fc_fail_reports == 0,
        severity="warning",
        expected=0,
        current=fc_fail_reports,
        message="feature canary artifacts contain fail status",
        allow_warn=True,
    )

    add(
        "feature_canary_latest_overall",
        fc_latest_overall in {STATUS_PASS, STATUS_WARN},
        severity="warning",
        expected="pass|warn",
        current=fc_latest_overall,
        message="latest feature canary report has fail status",
        allow_warn=True,
    )

    cb_reports = int(callback_latency.get("reports_in_month", 0))
    cb_overall_counts = (
        callback_latency.get("overall_counts", {})
        if isinstance(callback_latency.get("overall_counts"), dict)
        else {}
    )
    cb_fail_reports = int(cb_overall_counts.get(STATUS_FAIL, 0))
    cb_latest = callback_latency.get("latest") if isinstance(callback_latency.get("latest"), dict) else None
    cb_latest_overall = callback_latency.get("latest_overall")

    add(
        "callback_latency_latest_report_present",
        cb_latest is not None,
        severity="warning",
        expected="latest callback latency artifact exists",
        current=cb_latest.get("path") if cb_latest else None,
        message="callback latency artifact is missing",
        allow_warn=True,
    )

    add(
        "callback_latency_min_runs",
        cb_reports >= min_callback_latency_runs,
        severity="warning",
        expected=f">= {min_callback_latency_runs}",
        current=cb_reports,
        message="callback latency run count below policy threshold",
        allow_warn=True,
    )

    add(
        "callback_latency_no_fail_reports",
        cb_fail_reports == 0,
        severity="warning",
        expected=0,
        current=cb_fail_reports,
        message="callback latency artifacts contain fail status",
        allow_warn=True,
    )

    add(
        "callback_latency_latest_overall",
        cb_latest_overall in {STATUS_PASS, STATUS_WARN},
        severity="warning",
        expected="pass|warn",
        current=cb_latest_overall,
        message="latest callback latency report has fail status",
        allow_warn=True,
    )

    overall = STATUS_PASS
    for check in checks:
        overall = _combine_status(overall, str(check.get("status") or STATUS_WARN))

    return {"overall": overall, "checks": checks}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monthly reliability review pack generator")
    parser.add_argument("--project-root", default=".", help="Project root")
    parser.add_argument("--soak-dir", default="outputs/soak_reports", help="Soak report directory")
    parser.add_argument("--deploy-dir", default="outputs/deploy_guard", help="Deploy guard directory")
    parser.add_argument("--query-guard-dir", default="outputs/query_guard", help="Query-guard artifact directory")
    parser.add_argument(
        "--feature-canary-dir",
        default="outputs/feature_canary",
        help="Feature canary artifact directory",
    )
    parser.add_argument(
        "--callback-latency-dir",
        default="outputs/callback_latency",
        help="Callback latency artifact directory",
    )
    parser.add_argument("--output-dir", default="outputs/reliability/monthly", help="Monthly report output directory")
    parser.add_argument("--month", default=None, help="Target month (YYYY-MM), default=current month")
    parser.add_argument("--disk-path", action="append", default=[], help="Disk path to snapshot (repeatable)")
    parser.add_argument("--min-disk-free-gb", type=float, default=20.0, help="Warning threshold for min free GB")
    parser.add_argument("--backlog-p95-budget", type=float, default=20.0, help="WAL backlog p95 budget")
    parser.add_argument("--backlog-p99-budget", type=float, default=100.0, help="WAL backlog p99 budget")
    parser.add_argument(
        "--min-query-guard-runs",
        type=int,
        default=1,
        help="Minimum guarded query runs required in monthly window",
    )
    parser.add_argument(
        "--min-query-guard-suite-runs",
        type=int,
        default=1,
        help="Minimum query-guard suite runs required in monthly window",
    )
    parser.add_argument(
        "--min-feature-canary-runs",
        type=int,
        default=1,
        help="Minimum feature canary runs required in monthly window",
    )
    parser.add_argument(
        "--min-callback-latency-runs",
        type=int,
        default=1,
        help="Minimum callback latency runs required in monthly window",
    )
    parser.add_argument(
        "--run-drill-suite",
        action="store_true",
        help="Run drill command (pytest WAL outage drills) and record artifact",
    )
    parser.add_argument(
        "--drill-command",
        nargs="+",
        default=["uv", "run", "pytest", "--no-cov", "tests/integration/test_wal_outage_drills.py", "-q"],
        help="Drill command argv",
    )
    parser.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Exit 0 when monthly overall is warn",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        year, month = _parse_month(args.month)
    except ValueError as exc:
        print(f"[monthly] {exc}")
        return 2

    project_root = Path(args.project_root).resolve()
    soak_dir = Path(args.soak_dir)
    deploy_dir = Path(args.deploy_dir)
    query_guard_dir = Path(args.query_guard_dir)
    feature_canary_dir = Path(args.feature_canary_dir)
    callback_latency_dir = Path(args.callback_latency_dir)
    output_dir = Path(args.output_dir)

    start, end = _month_range(year, month)
    daily_reports = _collect_daily_reports(soak_dir, start, end)

    soak = _summarize_soak(daily_reports, soak_dir, year, month)
    backlog = _summarize_backlog(daily_reports)
    drift = _summarize_drift(deploy_dir, year, month)

    disk_paths = args.disk_path if args.disk_path else [".", ".wal", "data"]
    disk = _snapshot_disk(disk_paths, project_root)

    drill = _run_or_load_drill(
        output_dir=output_dir,
        project_root=project_root,
        run_drill_suite=bool(args.run_drill_suite),
        drill_command=[str(x) for x in args.drill_command],
    )

    release_channel = _summarize_release_channel(deploy_dir, year, month)
    query_guard = _summarize_query_guard(query_guard_dir, year, month)
    feature_canary = _summarize_feature_canary(feature_canary_dir, year, month)
    callback_latency = _summarize_callback_latency(callback_latency_dir, year, month)

    result = _evaluate(
        soak=soak,
        backlog=backlog,
        drift=drift,
        disk=disk,
        drill=drill,
        query_guard=query_guard,
        feature_canary=feature_canary,
        callback_latency=callback_latency,
        min_disk_free_gb=float(args.min_disk_free_gb),
        backlog_p95_budget=float(args.backlog_p95_budget),
        backlog_p99_budget=float(args.backlog_p99_budget),
        min_query_guard_runs=int(args.min_query_guard_runs),
        min_query_guard_suite_runs=int(args.min_query_guard_suite_runs),
        min_feature_canary_runs=int(args.min_feature_canary_runs),
        min_callback_latency_runs=int(args.min_callback_latency_runs),
    )

    month_str = f"{year:04d}-{month:02d}"
    report = {
        "generated_at": _now_iso(),
        "month": month_str,
        "scope_start": start.isoformat(),
        "scope_end": end.isoformat(),
        "sections": {
            "soak": soak,
            "backlog": backlog,
            "drift": drift,
            "disk": disk,
            "drill": drill,
            "release_channel": release_channel,
            "query_guard": query_guard,
            "feature_canary": feature_canary,
            "callback_latency": callback_latency,
        },
        "result": result,
    }

    base = f"monthly_{month_str}_{_stamp()}"
    json_path = output_dir / f"{base}.json"
    md_path = output_dir / f"{base}.md"
    _write_json(json_path, report)
    _write_markdown(md_path, report)

    print(f"[monthly] json: {json_path}")
    print(f"[monthly] md  : {md_path}")
    print(f"[monthly] overall: {result['overall']}")

    if result["overall"] == STATUS_FAIL:
        return 2
    if result["overall"] == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
