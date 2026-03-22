#!/usr/bin/env python3
"""Code canary gate -- post-deploy metric comparison against baseline."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

try:
    import structlog
    log: Any = structlog.get_logger("code_canary_gate")
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("code_canary_gate")

EXIT_PASS, EXIT_FAIL, EXIT_WARN = 0, 1, 2
STATUS_PASS, STATUS_FAIL, STATUS_WARN = "pass", "fail", "warn"
OUTPUT_DIR = Path("outputs/deploy_guard/canary")


@dataclass(frozen=True, slots=True)
class MetricDef:
    """A single canary metric check definition."""
    name: str
    query: str
    threshold_pct: float
    abs_threshold: float | None
    direction: str = "higher_is_bad"


METRIC_DEFS: Sequence[MetricDef] = (
    MetricDef("p99_latency",
              "histogram_quantile(0.99, rate(strategy_latency_ns_bucket[5m]))",
              threshold_pct=50.0, abs_threshold=None),
    MetricDef("error_rate",
              "rate(hft_errors_total[5m])",
              threshold_pct=100.0, abs_threshold=0.01),
    MetricDef("queue_depth_avg",
              "avg_over_time(hft_raw_queue_depth[5m])",
              threshold_pct=200.0, abs_threshold=None),
    MetricDef("event_loop_lag",
              "avg_over_time(hft_event_loop_lag_ms[5m])",
              threshold_pct=100.0, abs_threshold=None),
    MetricDef("feed_gap",
              "max_over_time(hft_feed_gap_seconds[5m])",
              threshold_pct=100.0, abs_threshold=30.0),
)


@dataclass(slots=True)
class CheckResult:
    """Result of a single metric comparison."""
    metric: str
    baseline_value: float | None
    current_value: float | None
    threshold_pct: float
    abs_threshold: float | None
    change_pct: float | None
    status: str


@dataclass(slots=True)
class GateReport:
    """Full canary gate report."""
    gate: str = "code_canary"
    result: str = STATUS_PASS
    timestamp: str = ""
    deploy_sha: str = ""
    baseline_sha: str = ""
    window_s: int = 300
    checks: list[dict[str, Any]] = field(default_factory=list)
    recommendation: str = "safe_to_promote"


_UTC = dt.timezone.utc

def _now_iso() -> str:
    return dt.datetime.now(_UTC).astimezone().isoformat()

def _stamp() -> str:
    return dt.datetime.now(_UTC).strftime("%Y%m%dT%H%M%SZ")


def _git_sha() -> str:
    """Return current HEAD SHA, or 'unknown' on failure."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _query_prometheus(prom_url: str, expr: str) -> float | None:
    """Query Prometheus instant query API; return scalar value or None."""
    url = f"{prom_url}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning("prometheus_query_failed", url=url, error=str(exc))
        return None
    if body.get("status") != "success":
        log.warning("prometheus_query_error", response=body)
        return None
    results = body.get("data", {}).get("result", [])
    if not results:
        return None
    try:
        return float(results[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _collect_current(prom_url: str) -> dict[str, float | None]:
    """Collect current values for all canary metrics."""
    metrics: dict[str, float | None] = {}
    for mdef in METRIC_DEFS:
        metrics[mdef.name] = _query_prometheus(prom_url, mdef.query)
    return metrics


def collect_snapshot(prom_url: str) -> dict[str, Any]:
    """Query all canary metrics and return a snapshot dict."""
    metrics = _collect_current(prom_url)
    for name, val in metrics.items():
        log.info("metric_collected", metric=name, value=val)
    return {"sha": _git_sha(), "timestamp": _now_iso(),
            "prometheus_url": prom_url, "metrics": metrics}


def _determine_status(mdef: MetricDef, b_val: float | None,
                      c_val: float | None, change_pct: float | None) -> str:
    """Determine pass/fail/warn for a single check."""
    if c_val is None:
        return STATUS_WARN
    if mdef.abs_threshold is not None and c_val > mdef.abs_threshold:
        return STATUS_FAIL
    if b_val is None:
        return STATUS_WARN
    if change_pct is not None and change_pct > mdef.threshold_pct:
        return STATUS_FAIL
    return STATUS_PASS


def evaluate_checks(baseline: dict[str, Any],
                    current_metrics: dict[str, float | None]) -> list[CheckResult]:
    """Compare current metrics against baseline."""
    b_metrics: dict[str, float | None] = baseline.get("metrics", {})
    results: list[CheckResult] = []
    for mdef in METRIC_DEFS:
        b_val, c_val = b_metrics.get(mdef.name), current_metrics.get(mdef.name)
        change_pct: float | None = None
        if b_val is not None and c_val is not None and b_val != 0:
            change_pct = ((c_val - b_val) / abs(b_val)) * 100.0
        results.append(CheckResult(
            metric=mdef.name, baseline_value=b_val, current_value=c_val,
            threshold_pct=mdef.threshold_pct, abs_threshold=mdef.abs_threshold,
            change_pct=round(change_pct, 2) if change_pct is not None else None,
            status=_determine_status(mdef, b_val, c_val, change_pct),
        ))
    return results


def build_report(checks: list[CheckResult], baseline_sha: str,
                 window_s: int) -> GateReport:
    """Aggregate individual checks into a gate report."""
    statuses = [c.status for c in checks]
    if STATUS_FAIL in statuses:
        overall, rec = STATUS_FAIL, "rollback_recommended"
    elif STATUS_WARN in statuses:
        overall, rec = STATUS_WARN, "manual_review"
    else:
        overall, rec = STATUS_PASS, "safe_to_promote"
    return GateReport(result=overall, timestamp=_now_iso(), deploy_sha=_git_sha(),
                      baseline_sha=baseline_sha, window_s=window_s,
                      checks=[asdict(c) for c in checks], recommendation=rec)


def _log_summary(checks: list[CheckResult], report: GateReport) -> None:
    for c in checks:
        log.info("check_result", metric=c.metric, status=c.status,
                 baseline=c.baseline_value, current=c.current_value,
                 change_pct=c.change_pct)
    log.info("gate_result", result=report.result,
             recommendation=report.recommendation)


def _finish(checks: list[CheckResult], baseline_sha: str,
            window_s: int) -> int:
    """Build report, write JSON, log summary, return exit code."""
    report = build_report(checks, baseline_sha, window_s)
    report_path = OUTPUT_DIR / f"canary_report_{_stamp()}.json"
    _write_json(report_path, asdict(report))
    log.info("report_written", path=str(report_path), result=report.result)
    _log_summary(checks, report)
    if report.result == STATUS_FAIL:
        return EXIT_FAIL
    return EXIT_WARN if report.result == STATUS_WARN else EXIT_PASS


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Capture baseline metrics snapshot."""
    if args.dry_run:
        log.info("dry_run", action="snapshot", metrics=[m.name for m in METRIC_DEFS])
        return EXIT_PASS
    snapshot = collect_snapshot(args.prometheus_url)
    out_path = Path(args.output)
    _write_json(out_path, snapshot)
    log.info("snapshot_saved", path=str(out_path), sha=snapshot["sha"])
    return EXIT_PASS


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Evaluate current metrics against a saved baseline."""
    if args.dry_run:
        log.info("dry_run", action="evaluate", metrics=[m.name for m in METRIC_DEFS])
        return EXIT_PASS
    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        log.error("baseline_not_found", path=str(baseline_path))
        return EXIT_FAIL
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    current = _collect_current(args.prometheus_url)
    checks = evaluate_checks(baseline, current)
    return _finish(checks, baseline.get("sha", "unknown"), args.window_s)


def cmd_auto(args: argparse.Namespace) -> int:
    """One-shot: snapshot, wait, then evaluate."""
    if args.dry_run:
        log.info("dry_run", action="auto", window_s=args.window_s,
                 metrics=[m.name for m in METRIC_DEFS])
        return EXIT_PASS
    log.info("auto_snapshot_start")
    baseline = collect_snapshot(args.prometheus_url)
    baseline_path = OUTPUT_DIR / "baseline.json"
    _write_json(baseline_path, baseline)
    log.info("auto_snapshot_saved", path=str(baseline_path))
    log.info("auto_waiting", window_s=args.window_s)
    time.sleep(args.window_s)
    current = _collect_current(args.prometheus_url)
    checks = evaluate_checks(baseline, current)
    return _finish(checks, baseline.get("sha", "unknown"), args.window_s)


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="code_canary_gate",
        description="Post-deploy metric comparison gate for HFT Platform",
    )
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Print what would be checked without querying Prometheus")
    parser.add_argument("--prometheus-url", default="http://localhost:9090",
                        help="Prometheus HTTP API base URL (default: http://localhost:9090)")
    subs = parser.add_subparsers(dest="command", required=True)

    snap = subs.add_parser("snapshot", help="Capture baseline metrics snapshot")
    snap.add_argument("--output", default=str(OUTPUT_DIR / "baseline.json"),
                      help="Output path for baseline JSON")

    ev = subs.add_parser("evaluate", help="Evaluate current metrics vs baseline")
    ev.add_argument("--baseline", required=True, help="Path to baseline JSON file")
    ev.add_argument("--window-s", type=int, default=300,
                    help="Observation window in seconds (default: 300)")

    au = subs.add_parser("auto", help="Snapshot + wait + evaluate (one-shot)")
    au.add_argument("--window-s", type=int, default=300,
                    help="Observation window in seconds (default: 300)")
    return parser


def main() -> int:
    """Entry point."""
    args = _build_parser().parse_args()
    dispatch = {"snapshot": cmd_snapshot, "evaluate": cmd_evaluate, "auto": cmd_auto}
    handler = dispatch.get(args.command)
    return handler(args) if handler else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
