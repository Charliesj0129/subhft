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


def _write_daily_markdown(report: dict[str, Any], path: Path) -> None:
    lines = []
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
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| id | status | severity | value | threshold | message |")
    lines.append("|---|---|---|---:|---|---|")
    for c in report["checks"]:
        val = c.get("value")
        if isinstance(val, (int, float)):
            val_s = _fmt_float(float(val))
        else:
            val_s = str(val)
        lines.append(
            f"| `{c['id']}` | `{c['status']}` | `{c['severity']}` | `{val_s}` | `{c.get('threshold', '')}` | {c.get('message', '')} |"
        )
    lines.append("")
    lines.append("## Services")
    lines.append("")
    lines.append("| service | state | health | restart_count |")
    lines.append("|---|---|---|---:|")
    for s in report.get("docker", {}).get("services", []):
        lines.append(
            f"| `{s['service']}` | `{s['state']}` | `{s['health']}` | `{s['restart_count']}` |"
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
    _write_daily_markdown(report, md_path)

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

    lines = []
    lines.append("# Weekly Soak Summary")
    lines.append("")
    lines.append(f"- generated_at: `{summary['generated_at']}`")
    lines.append(f"- window: `{summary['window_start']}` ~ `{summary['window_end']}`")
    lines.append(
        f"- days: `{summary['days']}` (pass={summary['pass_days']}, warn={summary['warn_days']}, fail={summary['fail_days']})"
    )
    lines.append("")
    lines.append("## Daily Overview")
    lines.append("")
    lines.append("| day | overall | pass | warn | fail | unknown |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for d, payload in rows:
        c = payload.get("summary", {}).get("counts", {})
        lines.append(
            f"| `{d.isoformat()}` | `{payload.get('summary', {}).get('overall', STATUS_UNKNOWN)}` | "
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
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[soak] weekly json: {json_path}")
    print(f"[soak] weekly md  : {md_path}")
    print(
        "[soak] summary   : "
        + f"days={summary['days']} pass={summary['pass_days']} warn={summary['warn_days']} fail={summary['fail_days']}"
    )
    return 0 if fail_days == 0 else 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Soak acceptance reports (daily + weekly)")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "daily":
        return _run_daily(args)
    if args.command == "weekly":
        return _run_weekly(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    sys.exit(main())
