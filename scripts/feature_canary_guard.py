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
    lines.append("# Feature Canary Guard Report")
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
            check_id="feature_shadow_mismatch_increase",
            title="Feature shadow mismatch increase",
            expr=f"sum(increase(feature_shadow_parity_mismatch_total[{window}])) or vector(0)",
            op="le",
            threshold=float(args.max_shadow_mismatch),
            severity="critical",
            note="shadow parity mismatch should be zero during canary",
        ),
        Rule(
            check_id="feature_quality_gap_increase",
            title="Feature quality gap increase",
            expr=f'sum(increase(feature_quality_flags_total{{flag="gap"}}[{window}])) or vector(0)',
            op="le",
            threshold=float(args.max_gap_flags),
            severity="warning",
            note="feature quality gap spikes indicate feed or state issues",
        ),
        Rule(
            check_id="feature_quality_out_of_order_increase",
            title="Feature quality out_of_order increase",
            expr=f'sum(increase(feature_quality_flags_total{{flag="out_of_order"}}[{window}])) or vector(0)',
            op="le",
            threshold=float(args.max_out_of_order_flags),
            severity="warning",
            note="out_of_order quality flags should stay bounded",
        ),
        Rule(
            check_id="feature_quality_partial_increase",
            title="Feature quality partial increase",
            expr=f'sum(increase(feature_quality_flags_total{{flag="partial"}}[{window}])) or vector(0)',
            op="le",
            threshold=float(args.max_partial_flags),
            severity="warning",
            note="partial quality flags should stay bounded",
        ),
        Rule(
            check_id="feature_plane_latency_p99_ns",
            title="Feature plane latency p99 (ns)",
            expr=f"histogram_quantile(0.99, sum(rate(feature_plane_latency_ns_bucket[{window}])) by (le))",
            op="le",
            threshold=float(args.max_latency_p99_ns),
            severity="warning",
            note="feature plane p99 latency should remain within budget",
        ),
        Rule(
            check_id="feature_plane_update_error_ratio",
            title="Feature plane update error ratio",
            expr=(
                '(sum(increase(feature_plane_updates_total{result="error"}['
                + window
                + '])) or vector(0)) / '
                '(clamp_min(sum(increase(feature_plane_updates_total{result=~"updated|emitted|error"}['
                + window
                + "])) or vector(0), 1))"
            ),
            op="le",
            threshold=float(args.max_update_error_ratio),
            severity="warning",
            note="feature update error ratio should remain low",
        ),
        Rule(
            check_id="feature_shadow_checks_increase",
            title="Feature shadow checks increase",
            expr=f'sum(increase(feature_shadow_parity_checks_total{{result="checked"}}[{window}])) or vector(0)',
            op="ge",
            threshold=float(args.min_shadow_checks),
            severity="warning",
            note="shadow parity checks should run during canary window",
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
        return "promote_canary_allowed"
    if overall == STATUS_WARN:
        return "hold_canary_and_investigate"
    return "rollback_or_disable_feature_canary"


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
    p = argparse.ArgumentParser(description="Feature-plane shadow/canary guard using Prometheus metrics.")
    p.add_argument("--prom-url", default="http://localhost:9091", help="Prometheus base URL")
    p.add_argument("--window", default="1h", help="PromQL range window, e.g. 30m / 1h / 6h")
    p.add_argument("--max-shadow-mismatch", type=float, default=0.0, help="Max increase of shadow mismatches")
    p.add_argument("--max-gap-flags", type=float, default=0.0, help="Max increase of feature quality gap flags")
    p.add_argument(
        "--max-out-of-order-flags",
        type=float,
        default=0.0,
        help="Max increase of feature quality out_of_order flags",
    )
    p.add_argument("--max-partial-flags", type=float, default=0.0, help="Max increase of feature quality partial flags")
    p.add_argument("--max-latency-p99-ns", type=float, default=50_000.0, help="Max feature_plane_latency_ns p99")
    p.add_argument("--max-update-error-ratio", type=float, default=0.01, help="Max feature update error ratio")
    p.add_argument("--min-shadow-checks", type=float, default=1.0, help="Min increase of shadow parity checks")
    p.add_argument("--output-dir", default="outputs/feature_canary", help="Output report directory")
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
            "max_shadow_mismatch": args.max_shadow_mismatch,
            "max_gap_flags": args.max_gap_flags,
            "max_out_of_order_flags": args.max_out_of_order_flags,
            "max_partial_flags": args.max_partial_flags,
            "max_latency_p99_ns": args.max_latency_p99_ns,
            "max_update_error_ratio": args.max_update_error_ratio,
            "min_shadow_checks": args.min_shadow_checks,
        },
    }
    output_dir = Path(args.output_dir)
    stamp = _stamp()
    json_path = output_dir / f"feature_canary_{stamp}.json"
    md_path = output_dir / f"feature_canary_{stamp}.md"
    _write_json(json_path, report)
    _write_markdown(md_path, report)

    print(f"[feature-canary] overall={result['overall']} recommendation={result['recommendation']}")
    print(f"[feature-canary] json={json_path}")
    print(f"[feature-canary] md={md_path}")

    overall = str(result["overall"])
    if overall == STATUS_FAIL:
        return 2
    if overall == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
