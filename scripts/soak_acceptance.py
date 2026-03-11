#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

try:
    from scripts.report_narrative import (
        compute_risk_score,
        diagnose_checks,
        executive_summary,
        format_status_icon,
        recommend_actions,
        render_trend_section,
    )
    _HAS_NARRATIVE = True
except ImportError:
    _HAS_NARRATIVE = False


# -- Inline narrative helpers (fallback when report_narrative unavailable) --

def _format_icon(status: str) -> str:
    return {"pass": "\u2705", "warn": "\u26a0\ufe0f", "fail": "\u274c"}.get(status, "\u2753")


def _compute_risk_score_inline(checks: list[dict[str, Any]]) -> int:
    score = 0
    for c in checks:
        st, sev = c.get("status", STATUS_UNKNOWN), c.get("severity", "warning")
        if st == STATUS_FAIL:
            score += 15 if sev == "critical" else 8
        elif st == STATUS_WARN:
            score += 5 if sev == "critical" else 3
        elif st == STATUS_UNKNOWN:
            score += 2
    return min(score, 100)


def _risk_label(score: int) -> str:
    if score <= 10:
        return "low"
    if score <= 35:
        return "moderate"
    return "high" if score <= 65 else "critical"


def _executive_summary_inline(
    checks: list[dict[str, Any]], services: list[dict[str, Any]], overall: str,
) -> str:
    total, fail_n = len(checks), sum(1 for c in checks if c.get("status") == STATUS_FAIL)
    warn_n = sum(1 for c in checks if c.get("status") == STATUS_WARN)
    pass_n = sum(1 for c in checks if c.get("status") == STATUS_PASS)
    svc_up = sum(1 for s in services if s.get("state") == "running")
    return (
        f"Overall status: **{overall}**. "
        f"{pass_n}/{total} checks passed, {warn_n} warnings, {fail_n} failures. "
        f"{svc_up}/{len(services)} services running."
    )


def _diagnose_checks_inline(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for c in checks:
        st = c.get("status", STATUS_PASS)
        if st in (STATUS_FAIL, STATUS_WARN, STATUS_UNKNOWN):
            results.append({
                "id": c.get("id", "unknown"), "status": st,
                "severity": c.get("severity", "warning"),
                "diagnosis": c.get("message") or f"Check returned {st}",
            })
    sev_order = {"critical": 0, "warning": 1}
    st_order = {STATUS_FAIL: 0, STATUS_WARN: 1, STATUS_UNKNOWN: 2}
    results.sort(key=lambda d: (sev_order.get(d["severity"], 2), st_order.get(d["status"], 3)))
    return results


def _recommend_actions_inline(diagnosed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"priority": "immediate" if d["severity"] == "critical" else "next-window",
         "action": f"Investigate {d['id']}: {d['diagnosis']}"}
        for d in diagnosed
    ]


def _weekly_trend_arrow(prev_overall: str | None, cur_overall: str) -> str:
    if prev_overall is None:
        return "\u2192"
    rank = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_UNKNOWN: 2, STATUS_FAIL: 3}
    prev_r, cur_r = rank.get(prev_overall, 2), rank.get(cur_overall, 2)
    if cur_r < prev_r:
        return "\u2191"
    return "\u2193" if cur_r > prev_r else "\u2192"


@dataclass(frozen=True)
class Rule:
    check_id: str
    title: str
    expr: str
    op: str
    threshold: float
    severity: str  # critical | warning
    note: str


class CommandBackend:
    def __init__(self, ssh_target: str | None = None, connect_timeout_s: int = 8):
        self.ssh_target = ssh_target
        self.connect_timeout_s = connect_timeout_s

    def run(self, command: str, cwd: str | None = None, timeout_s: int = 30) -> tuple[int, str, str]:
        cmd = command
        if cwd:
            if self.ssh_target:
                local_home_prefix = str(Path.home()) + "/"
                suffix: str | None = None
                if cwd.startswith("~/"):
                    suffix = cwd[2:]
                elif cwd.startswith(local_home_prefix):
                    suffix = cwd[len(local_home_prefix) :]
                if suffix is not None:
                    suffix = suffix.replace('"', '\\"')
                    cmd = f'cd "$HOME/{suffix}" && {command}'
                else:
                    cmd = f"cd {shlex.quote(cwd)} && {command}"
            else:
                cmd = f"cd {shlex.quote(cwd)} && {command}"

        if self.ssh_target:
            remote_cmd = f"bash -lc {shlex.quote(cmd)}"
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={self.connect_timeout_s}",
                self.ssh_target,
                remote_cmd,
            ]
        else:
            argv = ["bash", "-lc", cmd]

        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        return proc.returncode, proc.stdout, proc.stderr


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def _parse_day(day: str | None) -> dt.date:
    if not day:
        return dt.date.today()
    return dt.date.fromisoformat(day)


def _parse_compose_json(output: str) -> list[dict[str, Any]]:
    payload = output.strip()
    if not payload:
        return []
    if payload.startswith("["):
        data = json.loads(payload)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    rows: list[dict[str, Any]] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _query_prom_local(prom_url: str, expr: str, timeout_s: int = 8) -> float | None:
    query_url = prom_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    req = urllib.request.Request(query_url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        return None
    result = payload.get("data", {}).get("result", [])
    if not result:
        return None
    raw = result[0].get("value")
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    try:
        return float(raw[1])
    except Exception:
        return None


def _query_prom_via_backend(backend: CommandBackend, prom_url: str, expr: str) -> tuple[float | None, str | None]:
    if backend.ssh_target is None:
        try:
            return _query_prom_local(prom_url, expr), None
        except Exception as exc:
            return None, str(exc)

    cmd = (
        "curl -fsS --get "
        + f"--data-urlencode query={shlex.quote(expr)} "
        + shlex.quote(prom_url.rstrip("/") + "/api/v1/query")
    )
    rc, out, err = backend.run(cmd, timeout_s=10)
    if rc != 0:
        return None, (err.strip() or out.strip() or f"curl exit={rc}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc}"
    if payload.get("status") != "success":
        return None, "prometheus status!=success"
    result = payload.get("data", {}).get("result", [])
    if not result:
        return None, None
    raw = result[0].get("value")
    if not isinstance(raw, list) or len(raw) < 2:
        return None, "invalid value payload"
    try:
        return float(raw[1]), None
    except Exception as exc:
        return None, f"float parse error: {exc}"


def _eval(op: str, value: float, threshold: float) -> bool:
    if op == "le":
        return value <= threshold
    if op == "lt":
        return value < threshold
    if op == "ge":
        return value >= threshold
    if op == "gt":
        return value > threshold
    if op == "eq":
        return value == threshold
    raise ValueError(f"unsupported op: {op}")


def _status_from_rule(rule: Rule, value: float | None, err: str | None) -> tuple[str, str]:
    if value is None:
        return STATUS_UNKNOWN, err or "no series"
    ok = _eval(rule.op, value, rule.threshold)
    if ok:
        return STATUS_PASS, rule.note
    if rule.severity == "critical":
        return STATUS_FAIL, rule.note
    return STATUS_WARN, rule.note


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1000:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.2f}"
    return f"{value:.4f}"


def _load_previous_daily(output_dir: Path, day: dt.date) -> dict[str, Any] | None:
    daily_dir = output_dir / "daily"
    if not daily_dir.exists():
        return None
    candidates = sorted(daily_dir.glob("*.json"))
    for p in reversed(candidates):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d >= day:
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _parse_uptime_seconds(status: str) -> int | None:
    text = (status or "").strip()
    if not text:
        return None
    lower = text.lower()
    if not lower.startswith("up "):
        return None
    text = text[3:].strip()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    text = text.replace("About ", "").replace("about ", "")
    if text.lower().startswith("less than a second"):
        return 1
    if text.lower().startswith("a minute"):
        return 60
    if text.lower().startswith("an hour"):
        return 3600

    units = {
        "second": 1,
        "minute": 60,
        "hour": 3600,
        "day": 24 * 3600,
        "week": 7 * 24 * 3600,
        "month": 30 * 24 * 3600,
        "year": 365 * 24 * 3600,
    }
    matches = re.findall(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?", text, flags=re.IGNORECASE)
    if not matches:
        return None
    total = 0
    for raw_num, raw_unit in matches:
        unit = raw_unit.lower()
        factor = units.get(unit)
        if factor is None:
            continue
        total += int(raw_num) * factor
    return total or None


def _prom_window_literal(seconds: int) -> str:
    sec = max(60, int(seconds))
    if sec % 3600 == 0:
        return f"{sec // 3600}h"
    return f"{sec // 60}m"


def _derive_reconnect_window(services_raw: list[dict[str, Any]]) -> tuple[str, int | None]:
    default_window = "24h"
    min_window_s = 15 * 60
    max_window_s = 24 * 60 * 60
    for row in services_raw:
        service = str(row.get("Service") or row.get("Name") or "").lower()
        name = str(row.get("Name") or "").lower()
        if service != "hft-engine" and "hft-engine" not in name:
            continue
        uptime_s = _parse_uptime_seconds(str(row.get("Status") or ""))
        if uptime_s is None:
            return default_window, None
        window_s = max(min_window_s, min(max_window_s, uptime_s))
        return _prom_window_literal(window_s), uptime_s
    return default_window, None


def _daily_rules(expect_trading_day: bool, reconnect_window: str = "24h") -> list[Rule]:
    rules = [
        Rule(
            check_id="execution_gateway_alive_5m",
            title="ExecutionGateway alive (5m max)",
            expr="max_over_time(execution_gateway_alive[5m])",
            op="ge",
            threshold=1.0,
            severity="critical",
            note="ExecutionGateway 最近 5 分鐘需保持存活",
        ),
        Rule(
            check_id="execution_router_alive_5m",
            title="ExecutionRouter alive (5m max)",
            expr="max_over_time(execution_router_alive[5m])",
            op="ge",
            threshold=1.0,
            severity="critical",
            note="ExecutionRouter 最近 5 分鐘需保持存活",
        ),
        Rule(
            check_id="execution_gateway_uptime_ratio_24h",
            title="ExecutionGateway uptime ratio (24h avg)",
            expr="avg_over_time(execution_gateway_alive[24h])",
            op="ge",
            threshold=0.99,
            severity="warning",
            note="ExecutionGateway 24h 可用率應 >= 99%",
        ),
        Rule(
            check_id="execution_router_uptime_ratio_24h",
            title="ExecutionRouter uptime ratio (24h avg)",
            expr="avg_over_time(execution_router_alive[24h])",
            op="ge",
            threshold=0.99,
            severity="warning",
            note="ExecutionRouter 24h 可用率應 >= 99%",
        ),
        Rule(
            check_id="raw_queue_drops_24h",
            title="Raw queue drops (24h increase)",
            expr="sum(increase(raw_queue_dropped_total[24h])) or vector(0)",
            op="le",
            threshold=0.0,
            severity="warning",
            note="raw queue dropped 應為 0",
        ),
        Rule(
            check_id="feed_reconnect_timeout_24h",
            title="Feed reconnect timeout (24h increase)",
            expr="sum(increase(feed_reconnect_timeout_total[24h])) or vector(0)",
            op="le",
            threshold=0.0,
            severity="warning",
            note="reconnect timeout 不應持續增加",
        ),
        Rule(
            check_id="feed_reconnect_exception_24h",
            title="Feed reconnect exception (24h increase)",
            expr="sum(increase(feed_reconnect_exception_total[24h])) or vector(0)",
            op="le",
            threshold=0.0,
            severity="warning",
            note="reconnect exception 不應持續增加",
        ),
        Rule(
            check_id="feed_session_conflict_24h",
            title="Feed session conflict (24h increase)",
            expr="sum(increase(feed_session_conflict_total[24h])) or vector(0)",
            op="le",
            threshold=0.0,
            severity="warning",
            note="runtime session owner 不應衝突",
        ),
        Rule(
            check_id="feed_session_lease_lost_owner_24h",
            title="Feed session lease lost-owner (24h increase)",
            expr='sum(increase(feed_session_lease_ops_total{op="refresh",result="lost_owner"}[24h])) or vector(0)',
            op="le",
            threshold=0.0,
            severity="warning",
            note="lease refresh 不應失去 ownership",
        ),
        Rule(
            check_id="feed_session_lease_refresh_error_24h",
            title="Feed session lease refresh error (24h increase)",
            expr='sum(increase(feed_session_lease_ops_total{op="refresh",result="error"}[24h])) or vector(0)',
            op="le",
            threshold=0.0,
            severity="warning",
            note="lease refresh 不應連續失敗",
        ),
        Rule(
            check_id="session_lock_conflict_24h",
            title="Shioaji session lock conflict (24h increase)",
            expr="sum(increase(shioaji_session_lock_conflicts_total[24h])) or vector(0)",
            op="le",
            threshold=0.0,
            severity="warning",
            note="Shioaji session lock 不應衝突",
        ),
        Rule(
            check_id="login_fail_24h",
            title="Shioaji login fail (24h increase)",
            expr="sum(increase(shioaji_login_fail_total[24h])) or vector(0)",
            op="le",
            threshold=0.0,
            severity="warning",
            note="登入重試耗盡不應發生",
        ),
        Rule(
            check_id="feed_reconnect_failure_ratio_24h",
            title="Feed reconnect failure ratio (24h)",
            expr='(sum(increase(feed_reconnect_total{result=~"fail|exception"}[24h])) or vector(0)) / clamp_min((sum(increase(feed_reconnect_total{result=~"ok|fail|exception"}[24h])) or vector(0)), 1)',
            op="le",
            threshold=0.2,
            severity="warning",
            note="reconnect fail+exception 比例應 <= 20%",
        ),
        Rule(
            check_id="stormguard_halt_24h",
            title="StormGuard max mode (24h)",
            expr="max_over_time(stormguard_mode[24h])",
            op="le",
            threshold=2.0,
            severity="critical",
            note="不應進入 HALT(mode=3)",
        ),
        Rule(
            check_id="wal_backlog_max_24h",
            title="WAL backlog max (24h)",
            expr="max_over_time(wal_backlog_files[24h])",
            op="le",
            threshold=200.0,
            severity="warning",
            note="backlog 高峰不應過高",
        ),
        Rule(
            check_id="wal_drain_eta_max_24h",
            title="WAL drain ETA max (24h)",
            expr="max_over_time(wal_drain_eta_seconds[24h])",
            op="le",
            threshold=900.0,
            severity="warning",
            note="ETA 高峰應可控",
        ),
        Rule(
            check_id="wal_replay_errors_24h",
            title="WAL replay errors (24h increase)",
            expr="sum(increase(wal_replay_errors_total[24h])) or vector(0)",
            op="le",
            threshold=0.0,
            severity="warning",
            note="WAL replay error 不應增加",
        ),
        Rule(
            check_id="recorder_insert_failed_ratio_24h",
            title="Recorder insert failed ratio (24h)",
            expr=(
                '(sum(increase(recorder_insert_batches_total{result=~"failed_after_retry|failed_no_client"}[24h])) or vector(0)) '
                '/ clamp_min((sum(increase(recorder_insert_batches_total{result=~"success_no_retry|success_after_retry|failed_after_retry|failed_no_client"}[24h])) or vector(0)), 1)'
            ),
            op="le",
            threshold=0.005,
            severity="warning",
            note="Insert failed ratio SLO: daily <= 0.5%",
        ),
        Rule(
            check_id="recorder_insert_retry_ratio_24h",
            title="Recorder insert retry ratio (24h)",
            expr=(
                '(sum(increase(recorder_insert_batches_total{result=~"success_after_retry|failed_after_retry"}[24h])) or vector(0)) '
                '/ clamp_min((sum(increase(recorder_insert_batches_total{result=~"success_no_retry|success_after_retry|failed_after_retry|failed_no_client"}[24h])) or vector(0)), 1)'
            ),
            op="le",
            threshold=0.05,
            severity="warning",
            note="Insert retry ratio SLO: daily <= 5%",
        ),
        Rule(
            check_id="feed_reconnect_gap_24h",
            title=f"Feed reconnect gap count ({reconnect_window} increase)",
            expr=f'sum(increase(feed_reconnect_total{{result="gap"}}[{reconnect_window}])) or vector(0)',
            op="le",
            threshold=500.0,
            severity="warning",
            note="gap reconnect 應受控，避免重連風暴",
        ),
        Rule(
            check_id="feed_reconnect_symbol_gap_24h",
            title=f"Feed reconnect symbol_gap count ({reconnect_window} increase)",
            expr=f'sum(increase(feed_reconnect_total{{result="symbol_gap"}}[{reconnect_window}])) or vector(0)',
            op="le",
            threshold=5000.0,
            severity="warning",
            note="symbol_gap 應受控，避免 watchdog 過度觸發",
        ),
        Rule(
            check_id="quote_watchdog_callback_reregister_24h",
            title=f"Quote watchdog callback_reregister ({reconnect_window} increase)",
            expr=(
                "sum(increase(quote_watchdog_recovery_attempts_total"
                + '{action="callback_reregister"}'
                + f"[{reconnect_window}])) or vector(0)"
            ),
            op="le",
            threshold=120.0,
            severity="warning",
            note="No quote data; re-registering callbacks 應受控",
        ),
    ]
    if expect_trading_day:
        rules.append(
            Rule(
                check_id="feed_first_quote_24h",
                title="Feed first-quote callback (24h increase)",
                expr="sum(increase(feed_first_quote_total[24h])) or vector(0)",
                op="gt",
                threshold=0.0,
                severity="warning",
                note="交易日應至少出現 first quote callback",
            )
        )
    return rules


def _get_compose_services(backend: CommandBackend, project_root: str) -> tuple[list[dict[str, Any]], str | None]:
    rc, out, err = backend.run("docker compose ps --format json", cwd=project_root, timeout_s=20)
    if rc != 0:
        return [], (err.strip() or out.strip() or f"docker compose ps exit={rc}")
    return _parse_compose_json(out), None


def _get_restart_counts(backend: CommandBackend, project_root: str) -> dict[str, int]:
    cmd = (
        "ids=$(docker compose ps -q); "
        "for id in $ids; do "
        "name=$(docker inspect -f '{{.Name}}' \"$id\" | sed 's#^/##'); "
        "rc=$(docker inspect -f '{{.RestartCount}}' \"$id\"); "
        "echo \"$name $rc\"; "
        "done"
    )
    rc, out, _err = backend.run(cmd, cwd=project_root, timeout_s=20)
    if rc != 0:
        return {}
    result: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        name, val = parts
        try:
            result[name] = int(val)
        except ValueError:
            continue
    return result


def _compose_checks(services: list[dict[str, Any]], restart_counts: dict[str, int]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for svc in services:
        service = str(svc.get("Service") or svc.get("Name") or "unknown")
        state = str(svc.get("State") or "").lower()
        health = str(svc.get("Health") or "").lower()
        status = STATUS_PASS
        message = "running"
        if state != "running":
            status = STATUS_FAIL
            message = f"state={state or 'unknown'}"
        elif health and health != "healthy":
            status = STATUS_FAIL
            message = f"health={health}"
        checks.append(
            {
                "id": f"service_{service}",
                "title": f"Service {service} state",
                "status": status,
                "severity": "critical",
                "value": {"state": state or "unknown", "health": health or "n/a"},
                "threshold": "running+healthy",
                "message": message,
                "source": "docker",
            }
        )

        restart = restart_counts.get(str(svc.get("Name") or service))
        if restart is None:
            restart = restart_counts.get(service)
        checks.append(
            {
                "id": f"restart_{service}",
                "title": f"Service {service} restart count",
                "status": STATUS_PASS if (restart or 0) == 0 else STATUS_WARN,
                "severity": "warning",
                "value": restart if restart is not None else "n/a",
                "threshold": "0",
                "message": "restart_count should stay 0 during soak window",
                "source": "docker",
            }
        )
    return checks


def _prom_checks(
    backend: CommandBackend,
    prom_url: str,
    rules: Iterable[Rule],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rule in rules:
        value, err = _query_prom_via_backend(backend, prom_url, rule.expr)
        status, message = _status_from_rule(rule, value, err)
        rows.append(
            {
                "id": rule.check_id,
                "title": rule.title,
                "status": status,
                "severity": rule.severity,
                "value": value,
                "threshold": f"{rule.op} {rule.threshold}",
                "message": message,
                "expr": rule.expr,
                "source": "prometheus",
            }
        )
    return rows


def _apply_restart_delta(
    checks: list[dict[str, Any]],
    current_restart: dict[str, int],
    previous_report: dict[str, Any] | None,
) -> None:
    if not previous_report:
        return
    prev_restart = previous_report.get("docker", {}).get("restart_counts", {})
    if not isinstance(prev_restart, dict):
        return
    for check in checks:
        check_id = str(check.get("id", ""))
        if not check_id.startswith("restart_"):
            continue
        service = check_id.replace("restart_", "", 1)
        curr = current_restart.get(service)
        prev = prev_restart.get(service)
        if curr is None or prev is None:
            continue
        delta = curr - int(prev)
        check["delta_from_prev_day"] = delta
        if delta > 0:
            check["status"] = STATUS_WARN
            check["message"] = f"restart delta +{delta} from previous daily report"


def _summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_UNKNOWN: 0}
    for c in checks:
        counts[str(c.get("status", STATUS_UNKNOWN))] += 1
    overall = STATUS_PASS
    if counts[STATUS_FAIL] > 0:
        overall = STATUS_FAIL
    elif counts[STATUS_WARN] > 0 or counts[STATUS_UNKNOWN] > 0:
        overall = STATUS_WARN
    return {"overall": overall, "counts": counts}


def _combine_status(current: str, incoming: str) -> str:
    order = {
        STATUS_PASS: 0,
        STATUS_UNKNOWN: 1,
        STATUS_WARN: 2,
        STATUS_FAIL: 3,
    }
    if order.get(incoming, 1) > order.get(current, 1):
        return incoming
    return current


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    q_clamped = min(1.0, max(0.0, float(q)))
    pos = q_clamped * (len(xs) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(xs) - 1)
    frac = pos - lower
    return xs[lower] + (xs[upper] - xs[lower]) * frac


def _is_trading_day_payload(day: dt.date, payload: dict[str, Any]) -> bool:
    raw = payload.get("expect_trading_day")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return day.weekday() < 5


def _load_recent_daily_reports(
    output_dir: Path,
    day: dt.date,
    window_days: int,
) -> list[tuple[dt.date, dict[str, Any]]]:
    daily_dir = output_dir / "daily"
    rows: list[tuple[dt.date, dict[str, Any]]] = []
    for p in sorted(daily_dir.glob("*.json")):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d > day:
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append((d, payload))
    if window_days <= 0:
        return rows
    return rows[-window_days:]


def _evaluate_canary_window(
    rows: list[tuple[dt.date, dict[str, Any]]],
    *,
    min_trading_days: int,
    min_first_quote_pass_ratio: float,
    max_reconnect_failure_ratio: float,
    max_watchdog_callback_reregister: float,
) -> dict[str, Any]:
    trading_days = 0
    first_quote_pass_days = 0
    reconnect_values: list[float] = []
    watchdog_reregister_values: list[float] = []
    per_day: list[dict[str, Any]] = []
    overall = STATUS_PASS
    reasons: list[str] = []

    for d, payload in rows:
        is_trading = _is_trading_day_payload(d, payload)
        checks = payload.get("checks", [])
        checks_by_id: dict[str, dict[str, Any]] = {}
        if isinstance(checks, list):
            for item in checks:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("id") or "").strip()
                if cid:
                    checks_by_id[cid] = item

        first_quote = checks_by_id.get("feed_first_quote_24h", {})
        reconnect_ratio = checks_by_id.get("feed_reconnect_failure_ratio_24h", {})
        watchdog_reregister = checks_by_id.get("quote_watchdog_callback_reregister_24h", {})

        fq_status = str(first_quote.get("status") or STATUS_UNKNOWN)
        fq_value = _coerce_float(first_quote.get("value"))
        rr_status = str(reconnect_ratio.get("status") or STATUS_UNKNOWN)
        rr_value = _coerce_float(reconnect_ratio.get("value"))
        wd_status = str(watchdog_reregister.get("status") or STATUS_UNKNOWN)
        wd_value = _coerce_float(watchdog_reregister.get("value"))

        fq_ok = bool(fq_status == STATUS_PASS and fq_value is not None and fq_value > 0.0)
        rr_ok = bool(rr_value is not None and rr_value <= max_reconnect_failure_ratio)
        wd_ok = bool(wd_value is not None and wd_value <= max_watchdog_callback_reregister)
        day_status = STATUS_PASS

        if fq_status in {STATUS_FAIL, STATUS_WARN, STATUS_UNKNOWN}:
            day_status = _combine_status(day_status, fq_status)
        if rr_status in {STATUS_FAIL, STATUS_WARN, STATUS_UNKNOWN}:
            day_status = _combine_status(day_status, rr_status)
        if wd_status in {STATUS_FAIL, STATUS_WARN, STATUS_UNKNOWN}:
            day_status = _combine_status(day_status, wd_status)
        if rr_value is None:
            day_status = _combine_status(day_status, STATUS_UNKNOWN)
        elif not rr_ok:
            day_status = _combine_status(day_status, STATUS_FAIL)
        if wd_value is None:
            day_status = _combine_status(day_status, STATUS_UNKNOWN)
        elif not wd_ok:
            day_status = _combine_status(day_status, STATUS_FAIL)

        if is_trading:
            trading_days += 1
            if fq_ok:
                first_quote_pass_days += 1
            if rr_value is not None:
                reconnect_values.append(rr_value)
        if wd_value is not None:
            watchdog_reregister_values.append(wd_value)

        per_day.append(
            {
                "day": d.isoformat(),
                "is_trading_day": is_trading,
                "first_quote_value": fq_value,
                "first_quote_status": fq_status,
                "reconnect_failure_ratio": rr_value,
                "reconnect_ratio_status": rr_status,
                "watchdog_callback_reregister": wd_value,
                "watchdog_status": wd_status,
                "day_status": day_status,
            }
        )

    first_quote_pass_ratio = (
        float(first_quote_pass_days) / float(trading_days) if trading_days > 0 else 0.0
    )
    reconnect_ratio_max = max(reconnect_values) if reconnect_values else None
    reconnect_ratio_p95 = _quantile(reconnect_values, 0.95)
    watchdog_reregister_max = (
        max(watchdog_reregister_values) if watchdog_reregister_values else None
    )
    watchdog_reregister_p95 = _quantile(watchdog_reregister_values, 0.95)

    if trading_days < max(1, int(min_trading_days)):
        overall = _combine_status(overall, STATUS_WARN)
        reasons.append(
            f"insufficient trading days in window: {trading_days} < required {int(min_trading_days)}"
        )

    if first_quote_pass_ratio < float(min_first_quote_pass_ratio):
        overall = _combine_status(overall, STATUS_FAIL)
        reasons.append(
            f"first quote pass ratio {first_quote_pass_ratio:.3f} < required {float(min_first_quote_pass_ratio):.3f}"
        )

    if reconnect_ratio_max is None:
        overall = _combine_status(overall, STATUS_WARN)
        reasons.append("no reconnect failure ratio samples found in trading-day reports")
    elif reconnect_ratio_max > float(max_reconnect_failure_ratio):
        overall = _combine_status(overall, STATUS_FAIL)
        reasons.append(
            "reconnect failure ratio max "
            + f"{reconnect_ratio_max:.4f} > threshold {float(max_reconnect_failure_ratio):.4f}"
        )
    if watchdog_reregister_max is None:
        overall = _combine_status(overall, STATUS_WARN)
        reasons.append("no watchdog callback-reregister samples found in daily reports")
    elif watchdog_reregister_max > float(max_watchdog_callback_reregister):
        overall = _combine_status(overall, STATUS_FAIL)
        reasons.append(
            "watchdog callback_reregister max "
            + f"{watchdog_reregister_max:.4f} > threshold {float(max_watchdog_callback_reregister):.4f}"
        )

    return {
        "overall": overall,
        "trading_days": trading_days,
        "first_quote_pass_days": first_quote_pass_days,
        "first_quote_pass_ratio": first_quote_pass_ratio,
        "reconnect_failure_ratio_max": reconnect_ratio_max,
        "reconnect_failure_ratio_p95": reconnect_ratio_p95,
        "watchdog_callback_reregister_max": watchdog_reregister_max,
        "watchdog_callback_reregister_p95": watchdog_reregister_p95,
        "reasons": reasons,
        "daily": per_day,
    }


def _write_daily_markdown(
    report: dict[str, Any],
    path: Path,
    output_dir: Path | None = None,
) -> None:
    checks = report["checks"]
    services = report.get("docker", {}).get("services", [])
    overall = report["summary"]["overall"]

    lines: list[str] = []
    lines.append("# Daily Soak Acceptance Report")
    lines.append("")
    lines.append(f"- generated_at: `{report['generated_at']}`")
    lines.append(f"- scope_date: `{report['scope_date']}`")
    lines.append(f"- host: `{report['host']}`")
    lines.append(f"- overall: `{report['summary']['overall']}`")
    lines.append("")
    counts = report["summary"]["counts"]
    lines.append(
        f"- counts: pass={counts['pass']}, warn={counts['warn']}, fail={counts['fail']}, unknown={counts['unknown']}"
    )

    # --- Narrative: Executive Summary ---
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    if _HAS_NARRATIVE:
        lines.append(executive_summary(
            checks, services, overall,
            report.get("scope_date", ""), report.get("expect_trading_day", True),
        ))
        risk = compute_risk_score(checks)
    else:
        lines.append(_executive_summary_inline(checks, services, overall))
        risk = _compute_risk_score_inline(checks)
    lines.append("")
    lines.append(f"Risk Score: {risk}/100 ({_risk_label(risk)})")

    # --- Narrative: Trend Analysis ---
    if output_dir is not None:
        if _HAS_NARRATIVE:
            trend_text = render_trend_section(output_dir / "daily", report.get("scope_date", ""))
            if trend_text:
                lines.extend(["", "## Trend Analysis", "", trend_text])
        else:
            lines.extend(_render_trend_inline(output_dir / "daily", report.get("scope_date", ""), counts))

    # --- Narrative: Issues & Root Cause ---
    diagnosed = diagnose_checks(checks) if _HAS_NARRATIVE else _diagnose_checks_inline(checks)
    icon_fn = format_status_icon if _HAS_NARRATIVE else _format_icon
    if diagnosed:
        lines.extend(["", "## Issues & Root Cause", ""])
        for d in diagnosed:
            lines.append(f"- {icon_fn(d['status'])} **{d['id']}** ({d['severity']}): {d['diagnosis']}")

    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| id | status | severity | value | threshold | message |")
    lines.append("|---|---|---|---:|---|---|")
    for c in checks:
        val = c.get("value")
        if isinstance(val, (int, float)):
            val_s = _fmt_float(float(val))
        else:
            val_s = str(val)
        status_display = f"{icon_fn(c['status'])} {c['status']}"
        lines.append(
            f"| `{c['id']}` | `{status_display}` | `{c['severity']}` | `{val_s}` | `{c.get('threshold', '')}` | {c.get('message', '')} |"
        )
    lines.append("")
    lines.append("## Services")
    lines.append("")
    lines.append("| service | state | health | restart_count |")
    lines.append("|---|---|---|---:|")
    for s in services:
        lines.append(
            f"| `{s['service']}` | `{s['state']}` | `{s['health']}` | `{s['restart_count']}` |"
        )

    # --- Narrative: Recommendations ---
    recs = (recommend_actions(diagnosed) if _HAS_NARRATIVE else _recommend_actions_inline(diagnosed)) if diagnosed else []
    if recs:
        lines.append("")
        lines.append("## Recommendations")
        lines.append("")
        for i, rec in enumerate(recs, 1):
            lines.append(f"{i}. **[{rec['priority']}]** {rec['action']}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_trend_inline(
    daily_dir: Path, scope_date: str, cur_counts: dict[str, Any],
) -> list[str]:
    """Render a simple trend section comparing today vs yesterday."""
    try:
        current = dt.date.fromisoformat(scope_date)
    except (ValueError, TypeError):
        return []
    prev_path = daily_dir / f"{(current - dt.timedelta(days=1)).isoformat()}.json"
    if not prev_path.exists():
        return []
    try:
        prev_counts = json.loads(prev_path.read_text(encoding="utf-8")).get("summary", {}).get("counts", {})
    except Exception:
        return []
    lines = ["", "## Trend Analysis", ""]
    for key in ("pass", "warn", "fail"):
        cur_v, prev_v = int(cur_counts.get(key, 0)), int(prev_counts.get(key, 0))
        delta = cur_v - prev_v
        arrow = "\u2191" if delta > 0 else ("\u2193" if delta < 0 else "\u2192")
        lines.append(f"- {key}: {cur_v} {arrow} (delta: {delta:+d} vs previous day)")
    return lines


def _canary_promotion_verdict(result: dict[str, Any]) -> tuple[str, str]:
    """Return (verdict, justification) for canary promotion assessment."""
    overall = result.get("overall", STATUS_UNKNOWN)
    reasons = result.get("reasons") or []
    reason_text = "; ".join(reasons[:3]) if reasons else ""
    if overall == STATUS_PASS:
        return ("RECOMMEND PROMOTION",
                "All canary thresholds satisfied. Feed connectivity, quote delivery, and watchdog stability are within bounds.")
    if overall == STATUS_FAIL:
        return ("BLOCK \u2014 critical failures",
                f"Promotion blocked: {reason_text or 'threshold violations detected'}. Resolve before re-evaluation.")
    return ("HOLD \u2014 issues detected",
            f"Marginal results: {reason_text or 'marginal metrics detected'}. Monitor for another window.")


def _write_canary_markdown(report: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    lines.append("# Feed Canary Acceptance Report")
    lines.append("")
    lines.append(f"- generated_at: `{report['generated_at']}`")
    lines.append(f"- scope_end_day: `{report['scope_end_day']}`")
    lines.append(
        f"- window_days: `{report['window_days']}` (daily reports considered: `{report['considered_days']}`)"
    )
    lines.append(f"- overall: `{report['result']['overall']}`")

    # --- Narrative: Promotion Assessment ---
    result = report["result"]
    verdict, justification = _canary_promotion_verdict(result)
    risk = _compute_risk_score_inline([{"status": result["overall"], "severity": "critical"}])
    lines.extend(["", "## Promotion Assessment", "",
                   f"**{verdict}**", "", justification, "",
                   f"Risk Score: {risk}/100 ({_risk_label(risk)})"])
    th = report["thresholds"]
    lines.extend(["", "## Thresholds", "",
                   f"- min_trading_days: `{th['min_trading_days']}`",
                   f"- min_first_quote_pass_ratio: `{th['min_first_quote_pass_ratio']}`",
                   f"- max_reconnect_failure_ratio: `{th['max_reconnect_failure_ratio']}`",
                   f"- max_watchdog_callback_reregister: `{th['max_watchdog_callback_reregister']}`"])
    lines.extend(["", "## Result", "",
                   f"- trading_days: `{result['trading_days']}`",
                   f"- first_quote_pass_days: `{result['first_quote_pass_days']}`",
                   f"- first_quote_pass_ratio: `{_fmt_float(result['first_quote_pass_ratio'])}`",
                   f"- reconnect_failure_ratio_max: `{_fmt_float(result['reconnect_failure_ratio_max'])}`",
                   f"- reconnect_failure_ratio_p95: `{_fmt_float(result['reconnect_failure_ratio_p95'])}`",
                   f"- watchdog_callback_reregister_max: `{_fmt_float(result['watchdog_callback_reregister_max'])}`",
                   f"- watchdog_callback_reregister_p95: `{_fmt_float(result['watchdog_callback_reregister_p95'])}`"])
    lines.append("")
    lines.append("## Reasons")
    lines.append("")
    reasons = result.get("reasons") or []
    if reasons:
        for reason in reasons:
            lines.append(f"- {reason}")
    else:
        lines.append("- all thresholds satisfied")
    lines.append("")
    lines.append("## Daily Breakdown")
    lines.append("")
    lines.append(
        "| day | trading_day | first_quote | first_quote_status | reconnect_failure_ratio | reconnect_status | watchdog_callback_reregister | watchdog_status | day_status |"
    )
    lines.append("|---|---|---:|---|---:|---|---:|---|---|")
    for row in result.get("daily", []):
        lines.append(
            f"| `{row['day']}` | `{str(row['is_trading_day']).lower()}` | "
            f"`{_fmt_float(row['first_quote_value'])}` | `{row['first_quote_status']}` | "
            f"`{_fmt_float(row['reconnect_failure_ratio'])}` | `{row['reconnect_ratio_status']}` | "
            f"`{_fmt_float(row['watchdog_callback_reregister'])}` | `{row['watchdog_status']}` | "
            f"`{row['day_status']}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_daily(args: argparse.Namespace) -> int:
    day = _parse_day(args.day)
    output_dir = Path(args.output_dir)
    output_daily_dir = output_dir / "daily"
    output_daily_dir.mkdir(parents=True, exist_ok=True)

    backend = CommandBackend(args.ssh_target, args.ssh_connect_timeout_s)

    expect_trading_day = args.expect_trading_day
    if expect_trading_day == "auto":
        expect_trading = day.weekday() < 5
    else:
        expect_trading = expect_trading_day == "yes"

    services_raw, docker_err = _get_compose_services(backend, args.project_root)
    restart_counts = _get_restart_counts(backend, args.project_root)
    reconnect_window, engine_uptime_s = _derive_reconnect_window(services_raw)
    services = []
    for row in services_raw:
        service_name = str(row.get("Service") or row.get("Name") or "unknown")
        container_name = str(row.get("Name") or service_name)
        state = str(row.get("State") or "unknown")
        health = str(row.get("Health") or "n/a")
        restart_count = restart_counts.get(container_name, restart_counts.get(service_name, 0))
        services.append(
            {
                "service": service_name,
                "container": container_name,
                "state": state,
                "health": health,
                "status": str(row.get("Status") or ""),
                "restart_count": int(restart_count),
            }
        )

    checks = []
    if docker_err:
        checks.append(
            {
                "id": "docker_compose_ps",
                "title": "docker compose ps",
                "status": STATUS_FAIL,
                "severity": "critical",
                "value": "error",
                "threshold": "ok",
                "message": docker_err,
                "source": "docker",
            }
        )
    else:
        checks.extend(_compose_checks(services_raw, restart_counts))

    checks.extend(_prom_checks(backend, args.prom_url, _daily_rules(expect_trading, reconnect_window=reconnect_window)))
    previous = _load_previous_daily(output_dir, day)
    _apply_restart_delta(checks, restart_counts, previous)

    summary = _summary(checks)
    report = {
        "generated_at": _now_iso(),
        "scope_date": day.isoformat(),
        "host": socket.gethostname() if backend.ssh_target is None else backend.ssh_target,
        "ssh_target": backend.ssh_target,
        "project_root": args.project_root,
        "prom_url": args.prom_url,
        "expect_trading_day": expect_trading,
        "effective_reconnect_window": reconnect_window,
        "hft_engine_uptime_seconds": engine_uptime_s,
        "summary": summary,
        "checks": checks,
        "docker": {
            "services": services,
            "restart_counts": restart_counts,
            "compose_error": docker_err,
        },
    }

    json_path = output_daily_dir / f"{day.isoformat()}.json"
    md_path = output_daily_dir / f"{day.isoformat()}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_daily_markdown(report, md_path, output_dir=output_dir)

    print(f"[soak] daily json: {json_path}")
    print(f"[soak] daily md  : {md_path}")
    print(f"[soak] overall   : {summary['overall']}")

    if summary["overall"] == STATUS_FAIL:
        return 2
    if summary["overall"] == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


def _run_weekly(args: argparse.Namespace) -> int:
    day = _parse_day(args.day)
    output_dir = Path(args.output_dir)
    daily_dir = output_dir / "daily"
    weekly_dir = output_dir / "weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for p in sorted(daily_dir.glob("*.json")):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d > day:
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append((d, payload))
    rows = rows[-7:]
    if not rows:
        print("[soak] no daily reports found")
        return 1

    fail_days = 0
    warn_days = 0
    per_check_fail: dict[str, int] = {}
    for _d, payload in rows:
        overall = payload.get("summary", {}).get("overall", STATUS_UNKNOWN)
        if overall == STATUS_FAIL:
            fail_days += 1
        elif overall in {STATUS_WARN, STATUS_UNKNOWN}:
            warn_days += 1
        for c in payload.get("checks", []):
            if c.get("status") == STATUS_FAIL:
                cid = str(c.get("id"))
                per_check_fail[cid] = per_check_fail.get(cid, 0) + 1

    start_day = rows[0][0]
    end_day = rows[-1][0]
    summary = {
        "generated_at": _now_iso(),
        "window_start": start_day.isoformat(),
        "window_end": end_day.isoformat(),
        "days": len(rows),
        "fail_days": fail_days,
        "warn_days": warn_days,
        "pass_days": len(rows) - fail_days - warn_days,
        "top_failing_checks": sorted(per_check_fail.items(), key=lambda x: (-x[1], x[0]))[:10],
    }

    basename = f"week_{start_day.isoformat()}_{end_day.isoformat()}"
    json_path = weekly_dir / f"{basename}.json"
    md_path = weekly_dir / f"{basename}.md"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines: list[str] = []
    lines.append("# Weekly Soak Summary")
    lines.append("")
    lines.append(f"- generated_at: `{summary['generated_at']}`")
    lines.append(f"- window: `{summary['window_start']}` ~ `{summary['window_end']}`")
    lines.append(
        f"- days: `{summary['days']}` (pass={summary['pass_days']}, warn={summary['warn_days']}, fail={summary['fail_days']})"
    )

    # --- Narrative summary ---
    healthy = summary["pass_days"]
    lines.append("")
    lines.append(
        f"This week: {healthy}/{summary['days']} days healthy, "
        f"{summary['warn_days']} warnings, {summary['fail_days']} failures."
    )

    lines.append("")
    lines.append("## Daily Overview")
    lines.append("")
    lines.append("| day | trend | overall | pass | warn | fail | unknown |")
    lines.append("|---|---|---|---:|---:|---:|---:|")
    prev_overall: str | None = None
    for d, payload in rows:
        c = payload.get("summary", {}).get("counts", {})
        cur_overall = payload.get("summary", {}).get("overall", STATUS_UNKNOWN)
        arrow = _weekly_trend_arrow(prev_overall, cur_overall)
        prev_overall = cur_overall
        lines.append(
            f"| `{d.isoformat()}` | {arrow} | `{cur_overall}` | "
            f"{int(c.get('pass', 0))} | {int(c.get('warn', 0))} | {int(c.get('fail', 0))} | {int(c.get('unknown', 0))} |"
        )
    lines.append("")
    lines.append("## Top Failing Checks")
    lines.append("")
    if summary["top_failing_checks"]:
        lines.append("| check_id | fail_days |")
        lines.append("|---|---:|")
        for cid, count in summary["top_failing_checks"]:
            lines.append(f"| `{cid}` | {count} |")
    else:
        lines.append("No failing checks in this window.")

    # --- Top Issues Analysis ---
    if per_check_fail:
        lines.append("")
        lines.append("## Top Issues Analysis")
        lines.append("")
        recurring = [
            (cid, cnt) for cid, cnt in sorted(per_check_fail.items(), key=lambda x: -x[1])
            if cnt >= 2
        ][:5]
        if recurring:
            for cid, cnt in recurring:
                lines.append(
                    f"- **{cid}**: failed {cnt}/{summary['days']} days "
                    f"\u2014 recurring issue, investigate root cause"
                )
        else:
            lines.append("No recurring failures (all failures were single-day occurrences).")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[soak] weekly json: {json_path}")
    print(f"[soak] weekly md  : {md_path}")
    print(
        "[soak] summary   : "
        + f"days={summary['days']} pass={summary['pass_days']} warn={summary['warn_days']} fail={summary['fail_days']}"
    )
    return 0 if fail_days == 0 else 2


def _run_canary(args: argparse.Namespace) -> int:
    day = _parse_day(args.day)
    output_dir = Path(args.output_dir)
    canary_dir = output_dir / "canary"
    canary_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_recent_daily_reports(output_dir, day, window_days=args.window_days)
    if not rows:
        print("[soak] no daily reports found for canary evaluation")
        return 1

    result = _evaluate_canary_window(
        rows,
        min_trading_days=args.min_trading_days,
        min_first_quote_pass_ratio=args.min_first_quote_pass_ratio,
        max_reconnect_failure_ratio=args.max_reconnect_failure_ratio,
        max_watchdog_callback_reregister=args.max_watchdog_callback_reregister,
    )
    start_day = rows[0][0].isoformat()
    end_day = rows[-1][0].isoformat()
    report = {
        "generated_at": _now_iso(),
        "scope_end_day": day.isoformat(),
        "window_days": int(args.window_days),
        "considered_days": len(rows),
        "window_start": start_day,
        "window_end": end_day,
        "thresholds": {
            "min_trading_days": int(args.min_trading_days),
            "min_first_quote_pass_ratio": float(args.min_first_quote_pass_ratio),
            "max_reconnect_failure_ratio": float(args.max_reconnect_failure_ratio),
            "max_watchdog_callback_reregister": float(args.max_watchdog_callback_reregister),
        },
        "result": result,
    }

    basename = f"canary_{start_day}_{end_day}"
    json_path = canary_dir / f"{basename}.json"
    md_path = canary_dir / f"{basename}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_canary_markdown(report, md_path)

    print(f"[soak] canary json: {json_path}")
    print(f"[soak] canary md  : {md_path}")
    print(f"[soak] canary     : {result['overall']}")

    if result["overall"] == STATUS_FAIL:
        return 2
    if result["overall"] == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Soak acceptance reports (daily + weekly + canary)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--day", default=None, help="Scope day YYYY-MM-DD (default: today)")
        p.add_argument("--output-dir", default="outputs/soak_reports", help="Report output directory")
        p.add_argument("--project-root", default=".", help="Project root containing docker-compose.yml")
        p.add_argument("--prom-url", default="http://localhost:9091", help="Prometheus base URL")
        p.add_argument("--ssh-target", default=None, help="Run command collection via SSH (e.g. user@host)")
        p.add_argument("--ssh-connect-timeout-s", type=int, default=8, help="SSH connect timeout in seconds")

    daily = sub.add_parser("daily", help="Generate one daily soak report")
    add_common(daily)
    daily.add_argument(
        "--expect-trading-day",
        choices=["auto", "yes", "no"],
        default="auto",
        help="Whether to enforce feed events increase check",
    )
    daily.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Exit with 0 when overall=warn",
    )

    weekly = sub.add_parser("weekly", help="Generate weekly summary from latest 7 daily reports")
    add_common(weekly)

    canary = sub.add_parser("canary", help="Evaluate feed canary thresholds from recent daily reports")
    add_common(canary)
    canary.add_argument(
        "--window-days",
        type=int,
        default=10,
        help="Number of recent daily reports to evaluate",
    )
    canary.add_argument(
        "--min-trading-days",
        type=int,
        default=5,
        help="Minimum trading-day reports required in window",
    )
    canary.add_argument(
        "--min-first-quote-pass-ratio",
        type=float,
        default=1.0,
        help="Minimum ratio of trading days with first-quote check pass",
    )
    canary.add_argument(
        "--max-reconnect-failure-ratio",
        type=float,
        default=0.2,
        help="Maximum allowed reconnect failure ratio in window",
    )
    canary.add_argument(
        "--max-watchdog-callback-reregister",
        type=float,
        default=120.0,
        help="Maximum allowed watchdog callback_reregister count in window",
    )
    canary.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Exit with 0 when overall=warn",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "daily":
        return _run_daily(args)
    if args.command == "weekly":
        return _run_weekly(args)
    if args.command == "canary":
        return _run_canary(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    sys.exit(main())
