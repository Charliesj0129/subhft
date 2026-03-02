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
    record.add_argument("--notes", default="")
    record.set_defaults(func=cmd_record_paper)

    summary = sub.add_parser("summarize-paper", help="Summarize paper-trade sessions")
    summary.add_argument("--alpha-id", required=True)
    summary.add_argument("--experiments-dir", default="research/experiments")
    summary.add_argument("--out", default=None)
    summary.set_defaults(func=cmd_summarize_paper)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
