#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
    severity: str  # critical|warning
    note: str


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_markdown(path: Path, report: dict[str, Any]) -> None:
    result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
    checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []
    lines: list[str] = []
    lines.append("# Callback Latency Guard Report")
    lines.append("")
    lines.append(f"- generated_at: `{report.get('generated_at')}`")
    lines.append(f"- window: `{report.get('window')}`")
    lines.append(f"- prom_url: `{report.get('prom_url')}`")
    lines.append(f"- overall: `{result.get('overall')}`")
    lines.append(f"- recommendation: `{result.get('recommendation')}`")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| id | status | severity | value | threshold | message |")
    lines.append("|---|---|---|---:|---:|---|")
    for row in checks:
        if not isinstance(row, dict):
            continue
        value = row.get("value")
        val_s = "n/a" if value is None else f"{float(value):.6g}"
        lines.append(
            f"| `{row.get('id')}` | `{row.get('status')}` | `{row.get('severity')}` | "
            f"`{val_s}` | `{row.get('threshold')}` | {row.get('message')} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _query_prom(prom_url: str, expr: str, timeout_s: int = 8) -> tuple[float | None, str | None]:
    url = prom_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return None, str(exc)
    if payload.get("status") != "success":
        return None, "prometheus status!=success"
    result = payload.get("data", {}).get("result", [])
    if not result:
        return None, None
    value = result[0].get("value")
    if not isinstance(value, list) or len(value) < 2:
        return None, "invalid value payload"
    try:
        return float(value[1]), None
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


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_UNKNOWN: 1, STATUS_WARN: 2, STATUS_FAIL: 3}
    return incoming if order.get(incoming, 0) > order.get(current, 0) else current


def _build_rules(args: argparse.Namespace) -> list[Rule]:
    window = args.window
    return [
        Rule(
            check_id="callback_ingress_latency_p99_ns",
            title="Callback ingress latency p99 (ns)",
            expr=(
                "histogram_quantile(0.99, "
                f"sum(rate(shioaji_quote_callback_ingress_latency_ns_bucket[{window}])) by (le))"
            ),
            op="le",
            threshold=float(args.max_callback_ingress_p99_ns),
            severity="critical",
            note="callback ingress p99 must stay within budget",
        ),
        Rule(
            check_id="callback_queue_dropped_increase",
            title="Callback queue dropped increase",
            expr=f"sum(increase(shioaji_quote_callback_queue_dropped_total[{window}])) or vector(0)",
            op="le",
            threshold=float(args.max_callback_queue_dropped),
            severity="critical",
            note="callback ingress queue should not drop payloads",
        ),
        Rule(
            check_id="callback_queue_depth_p99",
            title="Callback queue depth p99",
            expr=f"max(quantile_over_time(0.99, shioaji_quote_callback_queue_depth[{window}])) or vector(0)",
            op="le",
            threshold=float(args.max_callback_queue_depth_p99),
            severity="warning",
            note="callback ingress queue depth should stay bounded",
        ),
        Rule(
            check_id="callback_parse_fallback_ratio",
            title="Callback parse fallback ratio",
            expr=(
                '(sum(increase(market_data_callback_parse_total{result="fallback"}['
                + window
                + '])) or vector(0)) / '
                '(clamp_min(sum(increase(market_data_callback_parse_total{result=~"fast|fallback"}['
                + window
                + "])) or vector(0), 1))"
            ),
            op="le",
            threshold=float(args.max_parse_fallback_ratio),
            severity="warning",
            note="fallback parsing ratio should stay low",
        ),
        Rule(
            check_id="callback_parse_miss_increase",
            title="Callback parse miss increase",
            expr=f'sum(increase(market_data_callback_parse_total{{result="miss"}}[{window}])) or vector(0)',
            op="le",
            threshold=float(args.max_parse_miss),
            severity="critical",
            note="callback parser miss should remain zero",
        ),
        Rule(
            check_id="callback_parse_samples_increase",
            title="Callback parse sample increase",
            expr=f'sum(increase(market_data_callback_parse_total{{result=~"fast|fallback|miss"}}[{window}])) or vector(0)',
            op="ge",
            threshold=float(args.min_parse_samples),
            severity="warning",
            note="callback parse metrics should have enough samples in window",
        ),
    ]


def _evaluate_rule(rule: Rule, value: float | None, error: str | None) -> tuple[str, str]:
    if value is None:
        if error:
            if rule.severity == "critical":
                return STATUS_FAIL, f"{rule.note} ({error})"
            return STATUS_WARN, f"{rule.note} ({error})"
        return STATUS_WARN, f"{rule.note} (no series)"
    ok = _eval(rule.op, value, rule.threshold)
    if ok:
        return STATUS_PASS, rule.note
    if rule.severity == "critical":
        return STATUS_FAIL, rule.note
    return STATUS_WARN, rule.note


def _recommendation(overall: str) -> str:
    if overall == STATUS_PASS:
        return "callback_path_healthy"
    if overall == STATUS_WARN:
        return "investigate_callback_path"
    return "block_canary_and_rollback_callback_changes"


def _evaluate_rules(
    rules: list[Rule], query_fn: Callable[[str], tuple[float | None, str | None]]
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    overall = STATUS_PASS
    for rule in rules:
        value, err = query_fn(rule.expr)
        status, message = _evaluate_rule(rule, value, err)
        overall = _combine_status(overall, status)
        checks.append(
            {
                "id": rule.check_id,
                "title": rule.title,
                "status": status,
                "severity": "critical" if rule.severity == "critical" else "warning",
                "value": value,
                "threshold": rule.threshold,
                "op": rule.op,
                "expr": rule.expr,
                "message": message,
                "error": err,
            }
        )
    return {
        "overall": overall,
        "recommendation": _recommendation(overall),
        "checks": checks,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Callback ingress latency guard using Prometheus metrics.")
    p.add_argument("--prom-url", default="http://localhost:9091", help="Prometheus base URL")
    p.add_argument("--window", default="30m", help="PromQL range window, e.g. 10m / 30m / 1h")
    p.add_argument(
        "--max-callback-ingress-p99-ns",
        type=float,
        default=100_000.0,
        help="Max shioaji callback ingress p99 latency in ns",
    )
    p.add_argument(
        "--max-callback-queue-dropped",
        type=float,
        default=0.0,
        help="Max increase of callback queue dropped count",
    )
    p.add_argument(
        "--max-callback-queue-depth-p99",
        type=float,
        default=512.0,
        help="Max p99 callback queue depth",
    )
    p.add_argument(
        "--max-parse-fallback-ratio",
        type=float,
        default=0.10,
        help="Max fallback ratio for callback parser",
    )
    p.add_argument("--max-parse-miss", type=float, default=0.0, help="Max increase of callback parser miss")
    p.add_argument("--min-parse-samples", type=float, default=1.0, help="Min callback parse samples in window")
    p.add_argument("--output-dir", default="outputs/callback_latency", help="Output report directory")
    p.add_argument("--allow-warn-exit-zero", action="store_true", help="Return 0 when overall=warn")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    rules = _build_rules(args)

    def _query(expr: str) -> tuple[float | None, str | None]:
        return _query_prom(args.prom_url, expr)

    result = _evaluate_rules(rules, _query)
    report = {
        "generated_at": _now_iso(),
        "window": args.window,
        "prom_url": args.prom_url,
        "result": result,
        "thresholds": {
            "max_callback_ingress_p99_ns": args.max_callback_ingress_p99_ns,
            "max_callback_queue_dropped": args.max_callback_queue_dropped,
            "max_callback_queue_depth_p99": args.max_callback_queue_depth_p99,
            "max_parse_fallback_ratio": args.max_parse_fallback_ratio,
            "max_parse_miss": args.max_parse_miss,
            "min_parse_samples": args.min_parse_samples,
        },
    }
    output_dir = Path(args.output_dir)
    stamp = _stamp()
    json_path = output_dir / f"callback_latency_{stamp}.json"
    md_path = output_dir / f"callback_latency_{stamp}.md"
    _write_json(json_path, report)
    _write_markdown(md_path, report)

    print(f"[callback-latency] overall={result['overall']} recommendation={result['recommendation']}")
    print(f"[callback-latency] json={json_path}")
    print(f"[callback-latency] md={md_path}")

    overall = str(result["overall"])
    if overall == STATUS_FAIL:
        return 2
    if overall == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
