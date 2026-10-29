"""CLI entry points for calibration workflow.

Commands:
  calibrate    — run exponent sweep + held-out validation for one instrument

Note: data audit is a separate CLI available via `python -m research.calibration.audit`.
When data is insufficient or replay is stub-only, writes a fallback profile
with literature-default exponent and confidence="low" so downstream consumers
(Plan C) have something to read.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any
from datetime import UTC, datetime
from pathlib import Path

from research.calibration.config import CalibrationProfile, save_calibration_profile
from research.calibration.probe_strategy import PassiveQuoteProbe
from research.calibration.replay import ReplayNotReadyError, build_probe_replay_fn
from research.calibration.scoring import CalibrationScore, DailyFillSummary
from research.calibration.sweep import generate_candidates, sweep_exponent
from research.calibration.validate import (
    determine_confidence,
    split_days,
    validate_on_heldout,
)

# Optional: ClickHouse streaming when CLICKHOUSE_PASSWORD env var is set.
# Initialised at module load so the try/except stays outside any function.
_ch_source = None
_use_ch_streaming = False
try:
    import os as _os

    _ch_password = _os.getenv("CLICKHOUSE_PASSWORD", "")
    if _ch_password:
        from research.data_pipeline.reader import GovernedHbtSource, GovernedReader  # noqa: PLC0415

        # The governed reader (research v2 C1): trading-day windows, session rule, dedup v1.
        _ch_source = GovernedHbtSource(
            GovernedReader.connect(
                host=_os.getenv("CLICKHOUSE_HOST", "localhost"),
                port=int(_os.getenv("CLICKHOUSE_PORT", "8123")),
                user=_os.getenv("CLICKHOUSE_USER", "default"),
                password=_ch_password,
            )
        )
        _use_ch_streaming = True
except Exception:  # noqa: BLE001
    pass

# Literature defaults per `docs/architecture/why-custom-maker-backtest.md`:
# "Exponent 1.5-2.0 appropriate for shallow books (5-50 queue depth) like TAIFEX,
# vs. default 3.0 suited to deep US equity books (500-5000)."
LITERATURE_DEFAULT_EXPONENT = 1.5

# The sweep this CLI runs, named once so the provenance block records what ran.
_QUEUE_MODELS = ("power_prob", "power_prob2", "power_prob3", "log_prob")
_EXPONENT_GRID = (0.5, 3.0, 0.25)  # min, max, step
_PROBE: dict[str, int] = {"qty": 1, "max_pos": 3}


def _load_live_fills_from_audit(
    audit_path: Path, instrument: str,
) -> tuple[list[str], dict[str, DailyFillSummary]]:
    """Load usable calibration days from audit report.

    Returns (days, live_fills_placeholder). live_fills_placeholder is empty because
    per-day fill aggregation from parquets is not implemented (deferred future task).
    """
    if not audit_path.exists():
        return [], {}
    report = json.loads(audit_path.read_text())
    bucket = report["per_instrument"].get(instrument, {})
    days = bucket.get("usable_calibration_days", [])
    # NOTE: Real per-day fill counts require extending audit to emit DailyFillSummary
    # per day. For now, we return empty fills dict — caller will detect insufficiency.
    fills: dict[str, DailyFillSummary] = {}
    return days, fills


def _calibration_provenance(args: argparse.Namespace, **facts: Any) -> dict[str, Any]:
    """What produced a profile: the RunStamp of this process plus the facts the caller names.

    The ground-truth audit report is the hashed dataset; the L2 source is named in
    ``sweep_config`` (a directory or the governed ClickHouse reader), not hashed.
    """
    from hft_platform.alpha.evidence import canonical_sha256  # noqa: PLC0415
    from hft_platform.alpha.provenance import build_run_stamp  # noqa: PLC0415

    sweep_config = {
        "instrument": args.instrument,
        "l2_source": "clickhouse_governed_reader" if _use_ch_streaming else str(args.l2_data_dir),
        "latency_us": int(args.latency_us),
        "tick_size": float(args.tick_size),
        "lot_size": float(args.lot_size),
        "allow_stub": bool(args.allow_stub),
        "queue_models": list(_QUEUE_MODELS),
        "exponent_grid": list(_EXPONENT_GRID),
        "probe": dict(_PROBE),
    }
    stamp = build_run_stamp(data_paths=[str(args.audit_report)], config_hash=canonical_sha256(sweep_config))
    return {"run_stamp": stamp, "sweep_config": sweep_config, **facts}


def _write_fallback_profile(
    instrument: str,
    output: Path,
    reason: str,
    provenance: dict[str, Any] | None = None,
) -> None:
    """Write a low-confidence profile using literature defaults.

    Writes confidence="low" with exponent=LITERATURE_DEFAULT_EXPONENT so
    downstream consumers can still run with a sensible default while treating
    the profile as uncalibrated. Callers SHOULD branch on
    `confidence == "low"` AND `composite_score == 0.0` to detect fallback.
    """
    profile = CalibrationProfile(
        instrument=instrument,
        queue_model="power_prob",
        exponent=LITERATURE_DEFAULT_EXPONENT,
        calibration_date=datetime.now(UTC).date().isoformat(),
        data_days_used=0,
        held_out_days=0,
        composite_score=0.0,
        validation_scores=CalibrationScore(0.0, 0.0, 0.0, 0.0),
        confidence="low",
        expected_fill_rate_per_day=0.0,
        provenance=None if provenance is None else {**provenance, "fallback_reason": reason},
    )
    save_calibration_profile(profile, output)
    print(
        f"[{instrument}] Wrote UNCALIBRATED fallback profile to {output}\n"
        f"  exponent = {LITERATURE_DEFAULT_EXPONENT} (literature default for shallow books)\n"
        f"  confidence = low — reason: {reason}"
    )


def cmd_calibrate(args: argparse.Namespace) -> int:
    if args.allow_stub:
        print(
            f"[{args.instrument}] WARNING: --allow-stub enabled. Replay will run "
            f"in stub mode with n_fills=0 for all candidates. Calibration result "
            f"will be meaningless. Use only for pipeline testing.",
            file=sys.stderr,
        )
    days, live_fills = _load_live_fills_from_audit(args.audit_report, args.instrument)
    if len(days) < 5:
        reason = f"only {len(days)} usable calibration days (< 5 minimum)"
        print(
            f"[{args.instrument}] Data gap: {reason}. Writing fallback profile.",
            file=sys.stderr,
        )
        _write_fallback_profile(args.instrument, args.output, reason, _calibration_provenance(args))
        return 0

    if not live_fills:
        reason = "no per-day live fill aggregation available (deferred task)"
        print(
            f"[{args.instrument}] Replay ground truth unavailable: {reason}. "
            "Writing fallback profile.",
            file=sys.stderr,
        )
        _write_fallback_profile(args.instrument, args.output, reason, _calibration_provenance(args))
        return 0

    train_days, test_days = split_days(days, ratio=0.7)
    print(f"[{args.instrument}] train={len(train_days)} test={len(test_days)} days")

    candidates = generate_candidates(
        queue_models=list(_QUEUE_MODELS),
        exponent_min=_EXPONENT_GRID[0], exponent_max=_EXPONENT_GRID[1], exponent_step=_EXPONENT_GRID[2],
    )
    print(f"[{args.instrument}] {len(candidates)} candidates")

    replay_fn = build_probe_replay_fn(
        instrument=args.instrument,
        probe_factory=lambda: PassiveQuoteProbe(**_PROBE),
        l2_data_dir=args.l2_data_dir,
        latency_us=args.latency_us,
        tick_size=args.tick_size,
        lot_size=args.lot_size,
        allow_stub_execution=args.allow_stub,
        use_ch_streaming=_use_ch_streaming,
        ch_data_source=_ch_source,
    )

    try:
        sweep_result = sweep_exponent(
            instrument=args.instrument,
            candidates=candidates,
            calibration_days=train_days,
            live_fills=live_fills,
            run_replay=replay_fn,
        )
    except ReplayNotReadyError as exc:
        print(f"[{args.instrument}] Replay stub blocked execution: {exc}", file=sys.stderr)
        _write_fallback_profile(args.instrument, args.output,
                                 "replay.py is stub — order submission not wired",
                                 _calibration_provenance(args))
        return 0

    print(
        f"[{args.instrument}] best: {sweep_result.best_candidate.label()} "
        f"composite={sweep_result.best_score.composite():.3f}"
    )

    try:
        validation_score = validate_on_heldout(
            sweep_result=sweep_result,
            heldout_days=test_days,
            live_fills=live_fills,
            run_replay=replay_fn,
        )
    except ReplayNotReadyError as exc:
        print(f"[{args.instrument}] Replay stub blocked validation: {exc}", file=sys.stderr)
        _write_fallback_profile(args.instrument, args.output,
                                 "replay.py is stub — order submission not wired",
                                 _calibration_provenance(args))
        return 0

    composite = validation_score.composite()
    confidence = determine_confidence(days=len(train_days), score=composite)
    print(
        f"[{args.instrument}] validation composite={composite:.3f} confidence={confidence}"
    )

    # Co-locate artifacts with output profile: <output_parent>/../calibration/artifacts/<instrument>/
    # Default: config/research/calibration_profiles.yaml → research/calibration/artifacts/<instrument>/
    artifacts_dir = Path(args.artifacts_dir) / args.instrument
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "sweep_results.json").write_text(
        json.dumps(
            {
                "instrument": args.instrument,
                "best": {
                    "queue_model": sweep_result.best_candidate.queue_model,
                    "exponent": sweep_result.best_candidate.exponent,
                    "composite_score": sweep_result.best_score.composite(),
                },
                "all_results": [
                    {
                        "candidate": cand.label(),
                        "composite_score": score.composite(),
                        "components": asdict(score),
                    }
                    for cand, score in sweep_result.all_results
                ],
            },
            indent=2,
        )
    )
    (artifacts_dir / "validation_report.json").write_text(
        json.dumps(
            {
                "held_out_days": test_days,
                "composite_score": composite,
                "components": asdict(validation_score),
                "confidence": confidence,
            },
            indent=2,
        )
    )

    live_fill_rates = [f.n_fills for f in live_fills.values() if f.n_fills > 0]
    expected_rate = sum(live_fill_rates) / max(len(live_fill_rates), 1)

    profile = CalibrationProfile(
        instrument=args.instrument,
        queue_model=sweep_result.best_candidate.queue_model,
        exponent=sweep_result.best_candidate.exponent,
        calibration_date=datetime.now(UTC).date().isoformat(),
        data_days_used=len(train_days),
        held_out_days=len(test_days),
        composite_score=composite,
        validation_scores=validation_score,
        confidence=confidence,
        expected_fill_rate_per_day=expected_rate,
        provenance=_calibration_provenance(
            args,
            train_days=list(train_days),
            held_out_days=list(test_days),
            sweep_trials=len(candidates),
            sweep_best=sweep_result.best_candidate.label(),
        ),
    )
    save_calibration_profile(profile, args.output)
    print(f"[{args.instrument}] Wrote profile to {args.output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibration CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    cal = sub.add_parser("calibrate", help="Run exponent sweep + validation")
    cal.add_argument("--instrument", required=True)
    cal.add_argument(
        "--audit-report", type=Path,
        default=Path("research/calibration/artifacts/data_audit_report.json"),
    )
    cal.add_argument("--l2-data-dir", type=Path, default=Path("research/data/raw"))
    cal.add_argument("--latency-us", type=int, default=36000)
    cal.add_argument("--tick-size", type=float, default=1.0)
    cal.add_argument("--lot-size", type=float, default=1.0)
    cal.add_argument("--allow-stub", action="store_true",
                     help="Allow replay.py stub execution (for testing plumbing only)")
    cal.add_argument(
        "--output", type=Path,
        default=Path("config/research/calibration_profiles.yaml"),
    )
    cal.add_argument(
        "--artifacts-dir", type=Path,
        default=Path("research/calibration/artifacts"),
        help="Directory for sweep_results.json and validation_report.json artifacts",
    )
    cal.set_defaults(func=cmd_calibrate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
