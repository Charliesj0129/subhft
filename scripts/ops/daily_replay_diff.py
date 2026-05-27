#!/usr/bin/env python3
"""Loop_v1 L11 — daily live-vs-replay diff runner.

Runs the L4 replay harness (`hft_platform.replay.cli_runner.run_replay_session`)
against a target session and emits Prometheus textfile-collector metrics so
``node_exporter --collector.textfile`` (or the Pushgateway, when
``HFT_REPLAY_PUSHGATEWAY_URL`` is set) can publish stabilization KPIs:

  * ``hft_replay_match_pct{loop_id, strategy_id, eligibility, phase}`` -- daily
    parity score (0..100). Sentinel ``-1`` when ineligible / unrunnable.
  * ``hft_replay_divergence_count{loop_id, ...}`` -- ``n_replayed - n_live``
    intent count delta.
  * ``hft_replay_n_intents{loop_id, ..., source="live|replayed"}`` -- raw
    intent counts so dashboards can show fan-in.
  * ``hft_replay_first_divergence_idx{loop_id, ...}`` -- ``-1`` when no
    divergence; useful for distinguishing "matched 100%" from "we never
    diverged but the run only emitted N events".

Stabilization phase comes from the loop YAML / env var
``HFT_STABILIZATION_PHASE`` and is exported as a label so the
``replay_parity_alert`` rules can apply phase-specific thresholds (Sim 99%
vs Shadow/Live 95%) without rewriting expressions.

The script is intentionally idempotent and safe to re-run::

    make daily-replay-diff SESSION=2026-05-04
    make daily-replay-diff                              # default = today

Exits 0 when the parity report was written, regardless of match_pct (the
alert rule decides whether the score is acceptable). Exits 1 only on
fixture-missing / strategy-unbuildable / fatal IO errors so cron alarms
fire on infrastructure failures rather than on legitimately-low parity.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("daily_replay_diff")

DEFAULT_LOOP_ID = "r47_tmf_v1"
DEFAULT_STRATEGY_ID = "R47_MAKER_TMF"
DEFAULT_PHASE = os.environ.get("HFT_STABILIZATION_PHASE", "sim")

# Prometheus textfile-collector standard write directory; override via
# --prom-file or HFT_REPLAY_PROM_FILE for non-Linux deployments.
DEFAULT_PROM_FILE = os.environ.get(
    "HFT_REPLAY_PROM_FILE",
    "/var/lib/node_exporter/textfile_collector/hft_replay_match.prom",
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--session",
        default=date.today().isoformat(),
        help="Session date YYYY-MM-DD (default: today).",
    )
    p.add_argument(
        "--loop",
        default=os.environ.get("HFT_LOOP", DEFAULT_LOOP_ID),
        help="Loop ID (default: $HFT_LOOP or r47_tmf_v1).",
    )
    p.add_argument(
        "--strategy",
        default=os.environ.get("HFT_STRATEGY_ID", DEFAULT_STRATEGY_ID),
        help="Strategy ID (default: $HFT_STRATEGY_ID or R47_MAKER_TMF).",
    )
    p.add_argument(
        "--fixture",
        required=False,
        default=None,
        help="Path to WAL fixture archive. Required unless --skip-replay.",
    )
    p.add_argument(
        "--phase",
        default=DEFAULT_PHASE,
        choices=("sim", "shadow", "live"),
        help="Stabilization phase label (default: $HFT_STABILIZATION_PHASE or sim).",
    )
    p.add_argument(
        "--allow-pre-recorder",
        action="store_true",
        help="Forwarded to replay runner: produce a pre_recorder report instead of refusing.",
    )
    p.add_argument(
        "--out-root",
        default="outputs/replay",
        help="Replay report root (default: outputs/replay).",
    )
    p.add_argument(
        "--prom-file",
        default=DEFAULT_PROM_FILE,
        help="Prometheus textfile-collector .prom output (default: %(default)s).",
    )
    p.add_argument(
        "--no-prom",
        action="store_true",
        help="Skip writing the Prometheus textfile (useful for ad-hoc local runs).",
    )
    p.add_argument(
        "--skip-replay",
        action="store_true",
        help="Do not invoke the replay harness; only re-export metrics from existing report.json.",
    )
    return p.parse_args(argv)


def _resolve_session_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _settings_for(loop_id: str, strategy_id: str) -> dict[str, Any]:
    """Minimal settings dict the replay runner needs.

    The runner only consumes ``settings["loop_id"]`` and ``settings["strategy"]``;
    avoiding ``load_settings()`` keeps the daily cron isolated from broker
    config / secret loading.
    """
    return {
        "loop_id": loop_id,
        "strategy": {
            "id": strategy_id,
            "module": os.environ.get("HFT_STRATEGY_MODULE", "hft_platform.strategies.r47_maker"),
            "class": os.environ.get("HFT_STRATEGY_CLASS", "R47MakerStrategy"),
        },
    }


def _read_report(out_root: Path, session: date) -> dict[str, Any] | None:
    rpt = out_root / session.isoformat() / "report.json"
    if not rpt.exists():
        return None
    try:
        return json.loads(rpt.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Cannot read replay report at %s: %s", rpt, exc)
        return None


def _format_prom(report: dict[str, Any], *, loop_id: str, strategy_id: str, phase: str) -> str:
    """Render textfile-collector format. Empty/None match_pct → -1 sentinel."""
    eligibility = str(report.get("eligibility_status", "unknown"))
    match_pct_raw = report.get("match_pct")
    match_pct = -1.0 if match_pct_raw is None else float(match_pct_raw)
    n_live = int(report.get("n_live_intents", 0))
    n_replay = int(report.get("n_replayed_intents", 0))
    n_market = int(report.get("n_market_events", 0))
    first_div_raw = report.get("first_divergence_idx")
    first_div = -1 if first_div_raw is None else int(first_div_raw)
    # ``ok`` is the strict parity flag from the shared diff engine. Legacy
    # reports without it fall back to the match_pct>=95 heuristic so the gauge
    # is never silently 1 on an unknown-shape report.
    ok_raw = report.get("ok")
    ok_val = (1 if match_pct >= 95.0 else 0) if ok_raw is None else (1 if ok_raw else 0)

    def _labels() -> str:
        return (
            f'loop_id="{loop_id}",strategy_id="{strategy_id}",'
            f'eligibility="{eligibility}",phase="{phase}"'
        )

    lines = [
        "# HELP hft_replay_match_pct Daily live-vs-replay parity score (0..100, -1 if ineligible)",
        "# TYPE hft_replay_match_pct gauge",
        f"hft_replay_match_pct{{{_labels()}}} {match_pct:.4f}",
        "# HELP hft_replay_divergence_count Replayed intent count minus live intent count",
        "# TYPE hft_replay_divergence_count gauge",
        f"hft_replay_divergence_count{{{_labels()}}} {n_replay - n_live}",
        "# HELP hft_replay_n_intents Intent counts by source",
        "# TYPE hft_replay_n_intents gauge",
        f'hft_replay_n_intents{{{_labels()},source="live"}} {n_live}',
        f'hft_replay_n_intents{{{_labels()},source="replayed"}} {n_replay}',
        "# HELP hft_replay_n_market_events Market events processed by the harness",
        "# TYPE hft_replay_n_market_events gauge",
        f"hft_replay_n_market_events{{{_labels()}}} {n_market}",
        "# HELP hft_replay_first_divergence_idx Index of first parity divergence (-1 if none)",
        "# TYPE hft_replay_first_divergence_idx gauge",
        f"hft_replay_first_divergence_idx{{{_labels()}}} {first_div}",
        "# HELP hft_replay_ok Strict parity flag (1=ok, 0=any divergence/empty/schema/ordering)",
        "# TYPE hft_replay_ok gauge",
        f"hft_replay_ok{{{_labels()}}} {ok_val}",
    ]
    return "\n".join(lines) + "\n"


def _write_prom(text: str, prom_file: str) -> None:
    out = Path(prom_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(out)
    logger.info("Wrote %d bytes to %s", len(text), out)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    session = _resolve_session_date(args.session)
    out_root = Path(args.out_root)

    if args.skip_replay:
        report = _read_report(out_root, session)
        if report is None:
            logger.error("--skip-replay set but no report.json at %s", out_root / session.isoformat())
            return 1
    else:
        if not args.fixture:
            logger.error("--fixture required (or pass --skip-replay to re-export existing report)")
            return 1

        from hft_platform.replay.cli_runner import run_replay_session

        settings = _settings_for(args.loop, args.strategy)
        rc = run_replay_session(
            settings,
            session_date=session,
            fixture_path=args.fixture,
            allow_pre_recorder=args.allow_pre_recorder,
            out_root=str(out_root),
        )
        report = _read_report(out_root, session)
        if report is None:
            logger.error("Replay runner returned %d but no report.json found", rc)
            return 1

    if args.no_prom:
        logger.info(
            "Replay session=%s match_pct=%s eligibility=%s n_live=%s n_replay=%s",
            session.isoformat(),
            report.get("match_pct"),
            report.get("eligibility_status"),
            report.get("n_live_intents"),
            report.get("n_replayed_intents"),
        )
        return 0

    prom_text = _format_prom(report, loop_id=args.loop, strategy_id=args.strategy, phase=args.phase)
    try:
        _write_prom(prom_text, args.prom_file)
    except OSError as exc:
        logger.error("Cannot write %s: %s", args.prom_file, exc)
        return 1

    logger.info(
        "Replay parity exported: session=%s match_pct=%s phase=%s",
        session.isoformat(),
        report.get("match_pct"),
        args.phase,
    )

    # Fail closed: an eligible session whose strict parity flag is False
    # exits non-zero so cron / CI alarms on the divergence itself rather than
    # relying solely on a (mutable) Prometheus alert rule. Pre-recorder /
    # ineligible observation runs stay exit 0 — they never claim a pass.
    if str(report.get("eligibility_status")) == "eligible" and report.get("ok") is False:
        logger.error(
            "Replay parity FAILED CLOSED: session=%s mismatch_type=%s first_divergence_idx=%s",
            session.isoformat(),
            report.get("mismatch_type"),
            report.get("first_divergence_idx"),
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
