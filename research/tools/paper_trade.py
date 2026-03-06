from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from hft_platform.alpha.experiments import ExperimentTracker


def cmd_record_paper(args: argparse.Namespace) -> int:
    tracker = ExperimentTracker(base_dir=args.experiments_dir)
    out = tracker.log_paper_trade_session(
        alpha_id=str(args.alpha_id),
        started_at=str(args.started_at) if args.started_at else None,
        ended_at=str(args.ended_at) if args.ended_at else None,
        trading_day=str(args.trading_day) if args.trading_day else None,
        fills=int(args.fills),
        pnl_bps=float(args.pnl_bps),
        drift_alerts=int(args.drift_alerts),
        execution_reject_rate=float(args.execution_reject_rate),
        notes=str(args.notes or ""),
        session_id=str(args.session_id) if args.session_id else None,
        reject_rate_p95=(
            float(getattr(args, "reject_rate_p95")) if getattr(args, "reject_rate_p95", None) is not None else None
        ),
        regime=(str(getattr(args, "regime")) if getattr(args, "regime", None) else None),
    )
    print(f"[paper_trade] session logged: {out}")
    return 0


def cmd_summarize_paper(args: argparse.Namespace) -> int:
    tracker = ExperimentTracker(base_dir=args.experiments_dir)
    summary = tracker.summarize_paper_trade(str(args.alpha_id))
    payload: dict[str, Any] = dict(summary)

    out_path = Path(args.out) if args.out else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"[paper_trade] summary written: {out_path}")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _effective_reject_rate(summary: dict[str, Any]) -> tuple[float, str]:
    p95 = summary.get("execution_reject_rate_p95")
    if p95 is not None:
        try:
            return float(p95), "p95"
        except (TypeError, ValueError):
            pass
    return float(summary.get("execution_reject_rate_mean", 0.0)), "mean"


def _build_governance_report(summary: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    session_count = int(summary.get("session_count", 0))
    calendar_days = int(summary.get("calendar_span_days", 0))
    trading_days = int(summary.get("distinct_trading_days", 0))
    min_session_seconds = int(summary.get("min_session_duration_seconds", 0))
    invalid_duration_count = int(summary.get("invalid_session_duration_count", session_count))
    drift_alerts_total = int(summary.get("drift_alerts_total", 0))
    regimes_covered_raw = summary.get("regimes_covered", [])
    regimes_covered = [str(x) for x in regimes_covered_raw] if isinstance(regimes_covered_raw, list) else []

    reject_rate, reject_rate_source = _effective_reject_rate(summary)
    required_session_seconds = max(60, int(args.min_session_minutes) * 60)

    checks: dict[str, dict[str, Any]] = {
        "shadow_sessions": {
            "value": session_count,
            "min": int(args.min_shadow_sessions),
            "pass": session_count >= int(args.min_shadow_sessions),
        },
        "calendar_span_days": {
            "value": calendar_days,
            "min": int(args.min_calendar_days),
            "pass": calendar_days >= int(args.min_calendar_days),
        },
        "trading_days": {
            "value": trading_days,
            "min": int(args.min_trading_days),
            "pass": trading_days >= int(args.min_trading_days),
        },
        "session_duration": {
            "value": min_session_seconds,
            "min_seconds": required_session_seconds,
            "invalid_session_duration_count": invalid_duration_count,
            "pass": (invalid_duration_count == 0 and min_session_seconds >= required_session_seconds),
        },
        "drift_alerts": {
            "value": drift_alerts_total,
            "max": int(args.max_drift_alerts),
            "pass": drift_alerts_total <= int(args.max_drift_alerts),
        },
        "execution_reject_rate": {
            "value": reject_rate,
            "max": float(args.max_execution_reject_rate),
            "source": reject_rate_source,
            "pass": reject_rate <= float(args.max_execution_reject_rate),
        },
        "regime_span": {
            "value": len(regimes_covered),
            "covered": regimes_covered,
            "min": int(args.min_regimes),
            "pass": len(regimes_covered) >= int(args.min_regimes),
        },
    }
    passed = all(bool(item.get("pass", False)) for item in checks.values())
    return {
        "alpha_id": str(args.alpha_id),
        "passed": passed,
        "checks": checks,
        "summary": summary,
    }


def cmd_check_paper_governance(args: argparse.Namespace) -> int:
    tracker = ExperimentTracker(base_dir=args.experiments_dir)
    summary = tracker.summarize_paper_trade(str(args.alpha_id))
    payload = _build_governance_report(summary, args)

    out_path = Path(args.out) if args.out else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"[paper_trade] governance report written: {out_path}")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))

    if payload["passed"] or not bool(args.strict):
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paper-trade governance tooling.")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record-paper", help="Record one paper-trade session")
    record.add_argument("--alpha-id", required=True)
    record.add_argument("--experiments-dir", default="research/experiments")
    record.add_argument("--session-id", default=None)
    record.add_argument("--started-at", default=None, help="ISO timestamp")
    record.add_argument("--ended-at", default=None, help="ISO timestamp")
    record.add_argument("--trading-day", default=None, help="YYYY-MM-DD")
    record.add_argument("--fills", type=int, default=0)
    record.add_argument("--pnl-bps", type=float, default=0.0)
    record.add_argument("--drift-alerts", type=int, default=0)
    record.add_argument("--execution-reject-rate", type=float, default=0.0)
    record.add_argument("--reject-rate-p95", type=float, default=None)
    record.add_argument("--regime", default=None, help="Market regime label (e.g. trending/mean_reverting)")
    record.add_argument("--notes", default="")
    record.set_defaults(func=cmd_record_paper)

    summary = sub.add_parser("summarize-paper", help="Summarize paper-trade sessions")
    summary.add_argument("--alpha-id", required=True)
    summary.add_argument("--experiments-dir", default="research/experiments")
    summary.add_argument("--out", default=None)
    summary.set_defaults(func=cmd_summarize_paper)

    check = sub.add_parser("check-paper-governance", help="Evaluate paper-trade Gate E readiness")
    check.add_argument("--alpha-id", required=True)
    check.add_argument("--experiments-dir", default="research/experiments")
    check.add_argument("--min-shadow-sessions", type=int, default=5)
    check.add_argument("--min-calendar-days", type=int, default=7)
    check.add_argument("--min-trading-days", type=int, default=5)
    check.add_argument("--min-session-minutes", type=int, default=60)
    check.add_argument("--max-drift-alerts", type=int, default=0)
    check.add_argument("--max-execution-reject-rate", type=float, default=0.01)
    check.add_argument("--min-regimes", type=int, default=2)
    check.add_argument("--strict", action="store_true", help="Return non-zero exit code when governance fails")
    check.add_argument("--out", default=None)
    check.set_defaults(func=cmd_check_paper_governance)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
