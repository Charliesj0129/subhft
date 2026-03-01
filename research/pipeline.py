from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hft_platform.alpha.promotion import PromotionConfig, promote_alpha
from hft_platform.alpha.validation import ValidationConfig, run_alpha_validation
from research import factory

_STANDARD_VALIDATION_PROFILE = "standard"
_VM_UL6_VALIDATION_PROFILE = "vm_ul6"

# Keep in sync with argparse defaults in _add_common_run_args.
_PROFILE_BASELINE_DEFAULTS: dict[str, Any] = {
    "latency_profile_id": "sim_p95_v2026-02-26",
    "local_decision_pipeline_latency_us": 250,
    "submit_ack_latency_ms": 36.0,
    "modify_ack_latency_ms": 43.0,
    "cancel_ack_latency_ms": 47.0,
    "live_uplift_factor": 1.5,
    "maker_fee_bps": -0.2,
    "taker_fee_bps": 0.2,
    "stat_pvalue_threshold": 0.1,
    "min_stat_tests_pass": 2,
    "bootstrap_samples": 1000,
    "opt_signal_threshold_steps": 8,
    "opt_max_is_oos_gap": 1.0,
    "opt_min_neighbor_objective_ratio": 0.6,
    "opt_min_deflated_sharpe": -0.1,
    "required_data_provenance_fields": [],
    "data_ul": 2,
    "stress_latency_multiplier": 1.5,
    "stress_fee_multiplier": 1.5,
    "min_stress_sharpe_ratio": 0.5,
    "stress_drawdown_limit_multiplier": 1.25,
    "min_shadow_sessions": 5,
    "max_execution_reject_rate": 0.01,
    "min_paper_trade_calendar_days": 7,
    "min_paper_trade_trading_days": 5,
    "min_paper_trade_session_minutes": 30,
    "min_sharpe_oos_gate_d": 1.0,
    "max_abs_drawdown_gate_d": 0.2,
    "max_turnover_gate_d": 2.0,
    "max_correlation_gate_d": 0.7,
    "enforce_rust_benchmark_gate": False,
}

_VM_UL6_PROFILE_OVERRIDES: dict[str, Any] = {
    "latency_profile_id": "sim_stress_v2026-02-26",
    "local_decision_pipeline_latency_us": 1000,
    "submit_ack_latency_ms": 56.0,
    "modify_ack_latency_ms": 75.0,
    "cancel_ack_latency_ms": 70.0,
    "live_uplift_factor": 1.8,
    "maker_fee_bps": -0.05,
    "taker_fee_bps": 0.35,
    "stat_pvalue_threshold": 0.05,
    "min_stat_tests_pass": 3,
    "bootstrap_samples": 3000,
    "opt_signal_threshold_steps": 12,
    "opt_max_is_oos_gap": 0.5,
    "opt_min_neighbor_objective_ratio": 0.8,
    "opt_min_deflated_sharpe": 0.2,
    "required_data_provenance_fields": [
        "source",
        "generator",
        "seed",
        "created_at",
        "data_file",
        "split",
        "symbols",
    ],
    "data_ul": 6,
    "stress_latency_multiplier": 2.0,
    "stress_fee_multiplier": 2.0,
    "min_stress_sharpe_ratio": 0.7,
    "stress_drawdown_limit_multiplier": 1.0,
    "min_shadow_sessions": 20,
    "max_execution_reject_rate": 0.005,
    "min_paper_trade_calendar_days": 28,
    "min_paper_trade_trading_days": 20,
    "min_paper_trade_session_minutes": 60,
    "min_sharpe_oos_gate_d": 1.8,
    "max_abs_drawdown_gate_d": 0.10,
    "max_turnover_gate_d": 1.2,
    "max_correlation_gate_d": 0.5,
    "enforce_rust_benchmark_gate": True,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


@dataclass(frozen=True)
class PipelineRunReport:
    alpha_id: str
    owner: str
    mode: str
    promotable: bool
    started_at: str
    finished_at: str
    passed: bool
    factory_optimize_report: str
    audit_report: str
    validation_report: str
    promotion_report: str | None
    index_report: str
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_factory_optimize(
    out_path: Path,
    audit_out: Path,
    index_out: Path,
    *,
    data_paths: list[str] | None,
    allow_audit_warnings: bool,
    skip_clean: bool,
    skip_index: bool,
) -> int:
    return factory.cmd_optimize(
        SimpleNamespace(
            out=str(out_path),
            audit_out=str(audit_out),
            index_out=str(index_out),
            data=list(data_paths or []),
            allow_audit_warnings=allow_audit_warnings,
            skip_clean=skip_clean,
            skip_index=skip_index,
        )
    )


def _run_factory_index(out_path: Path) -> int:
    return factory.cmd_index(SimpleNamespace(out=str(out_path)))


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, tuple):
        left = list(left)
    if isinstance(right, tuple):
        right = list(right)
    return left == right


def _apply_validation_profile(args: argparse.Namespace, *, strict_mode: bool, notes: list[str]) -> None:
    raw_profile = str(getattr(args, "validation_profile", _STANDARD_VALIDATION_PROFILE)).strip().lower()
    if raw_profile == _STANDARD_VALIDATION_PROFILE:
        return

    if raw_profile != _VM_UL6_VALIDATION_PROFILE:
        raise ValueError(f"Unknown validation_profile: {raw_profile}")

    applied: list[str] = []
    for key, target in _VM_UL6_PROFILE_OVERRIDES.items():
        if not hasattr(args, key):
            continue
        baseline = _PROFILE_BASELINE_DEFAULTS.get(key)
        current = getattr(args, key)
        if baseline is not None and not _values_equal(current, baseline):
            continue
        setattr(args, key, target)
        applied.append(key)

    if not strict_mode:
        notes.append(
            "validation_profile=vm_ul6 requested in triage mode; output remains non-promotable by policy."
        )

    if applied:
        notes.append(
            "Applied validation profile vm_ul6 overrides: "
            + ", ".join(sorted(applied))
        )
    else:
        notes.append("validation_profile=vm_ul6 requested; no default-value fields were available to override.")


def _run_pipeline(args: argparse.Namespace, *, mode: str) -> int:
    root = Path(args.project_root).resolve()
    stamp = _stamp()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    optimize_path = out_dir / f"{args.alpha_id}_{stamp}_factory_optimize.json"
    audit_path = out_dir / f"{args.alpha_id}_{stamp}_audit.json"
    validate_path = out_dir / f"{args.alpha_id}_{stamp}_validate.json"
    promote_path = out_dir / f"{args.alpha_id}_{stamp}_promote.json"
    index_path = out_dir / f"{args.alpha_id}_{stamp}_index.json"
    report_path = out_dir / f"{args.alpha_id}_{stamp}_pipeline_report.json"

    strict_mode = mode == "strict"
    started_at = _now_iso()
    notes: list[str] = []

    allow_audit_warnings = bool(getattr(args, "allow_audit_warnings", False)) if not strict_mode else False
    allow_gate_fail = bool(getattr(args, "allow_gate_fail", False)) if not strict_mode else False
    skip_gate_b_tests = bool(getattr(args, "skip_gate_b_tests", False)) if not strict_mode else False
    no_promote = bool(getattr(args, "no_promote", False)) if not strict_mode else False
    force_promote = bool(getattr(args, "force_promote", False)) if not strict_mode else False
    _apply_validation_profile(args, strict_mode=strict_mode, notes=notes)

    optimize_rc = _run_factory_optimize(
        optimize_path,
        audit_path,
        index_path,
        data_paths=list(args.data),
        allow_audit_warnings=allow_audit_warnings,
        skip_clean=bool(args.skip_factory_clean),
        skip_index=True,
    )
    if optimize_rc != 0:
        notes.append("Factory optimize preflight failed; pipeline stopped before validation.")
        final = PipelineRunReport(
            alpha_id=args.alpha_id,
            owner=args.owner,
            mode=mode,
            promotable=False,
            started_at=started_at,
            finished_at=_now_iso(),
            passed=False,
            factory_optimize_report=str(optimize_path),
            audit_report=str(audit_path),
            validation_report="",
            promotion_report=None,
            index_report="",
            notes=notes,
        )
        _write_json(report_path, final.to_dict())
        print(f"[research.pipeline] report: {report_path}")
        return 2

    validation = run_alpha_validation(
        ValidationConfig(
            alpha_id=args.alpha_id,
            data_paths=list(args.data),
            is_oos_split=float(args.is_oos_split),
            signal_threshold=float(args.signal_threshold),
            max_position=int(args.max_position),
            min_sharpe_oos=float(args.min_sharpe_oos_gate_c),
            max_abs_drawdown=float(args.max_abs_drawdown_gate_c),
            min_turnover=float(args.min_turnover_gate_c),
            skip_gate_b_tests=skip_gate_b_tests,
            pytest_timeout_s=int(args.pytest_timeout_s),
            project_root=str(root),
            experiments_dir=args.experiments_dir,
            latency_profile_id=str(args.latency_profile_id),
            local_decision_pipeline_latency_us=int(args.local_decision_pipeline_latency_us),
            submit_ack_latency_ms=float(args.submit_ack_latency_ms),
            modify_ack_latency_ms=float(args.modify_ack_latency_ms),
            cancel_ack_latency_ms=float(args.cancel_ack_latency_ms),
            live_uplift_factor=float(args.live_uplift_factor),
            maker_fee_bps=float(args.maker_fee_bps),
            taker_fee_bps=float(args.taker_fee_bps),
            stat_pvalue_threshold=float(args.stat_pvalue_threshold),
            min_stat_tests_pass=int(args.min_stat_tests_pass),
            stat_correction_method=("bh" if strict_mode else "none"),
            min_stat_tests_bh_pass=(1 if strict_mode else int(args.min_stat_tests_pass)),
            enable_walk_forward=bool(strict_mode),
            enable_param_optimization=bool(strict_mode),
            opt_signal_threshold_min=float(args.opt_signal_threshold_min),
            opt_signal_threshold_max=float(args.opt_signal_threshold_max),
            opt_signal_threshold_steps=int(args.opt_signal_threshold_steps),
            opt_objective=str(args.opt_objective),
            opt_max_is_oos_gap=float(args.opt_max_is_oos_gap),
            opt_min_neighbor_objective_ratio=float(args.opt_min_neighbor_objective_ratio),
            opt_min_deflated_sharpe=float(args.opt_min_deflated_sharpe),
            require_paper_refs=bool(strict_mode),
            require_paper_index_link=bool(strict_mode),
            enforce_data_governance=bool(strict_mode),
            require_data_meta=bool(strict_mode),
            allowed_data_roots=tuple(str(x) for x in args.allowed_data_roots),
            required_data_provenance_fields=tuple(str(x) for x in args.required_data_provenance_fields),
            data_ul=int(args.data_ul),
            bootstrap_samples=int(args.bootstrap_samples),
            stress_latency_multiplier=float(args.stress_latency_multiplier),
            stress_fee_multiplier=float(args.stress_fee_multiplier),
            min_stress_sharpe_ratio=float(args.min_stress_sharpe_ratio),
            stress_drawdown_limit_multiplier=float(args.stress_drawdown_limit_multiplier),
        )
    )
    _write_json(validate_path, validation.to_dict())

    promotion_out: str | None = None
    promotion_passed = False
    if no_promote:
        notes.append("Promotion step skipped by --no-promote.")
    else:
        promotion = promote_alpha(
            PromotionConfig(
                alpha_id=args.alpha_id,
                owner=args.owner,
                project_root=str(root),
                experiments_dir=str(args.experiments_dir),
                scorecard_path=validation.scorecard_path,
                shadow_sessions=int(args.shadow_sessions),
                min_shadow_sessions=int(args.min_shadow_sessions),
                drift_alerts=int(args.drift_alerts),
                execution_reject_rate=float(args.execution_reject_rate),
                max_execution_reject_rate=float(args.max_execution_reject_rate),
                require_paper_trade_governance=bool(strict_mode),
                paper_trade_summary_path=(str(args.paper_trade_summary) if args.paper_trade_summary else None),
                min_paper_trade_calendar_days=int(args.min_paper_trade_calendar_days),
                min_paper_trade_trading_days=int(args.min_paper_trade_trading_days),
                min_paper_trade_session_minutes=int(args.min_paper_trade_session_minutes),
                min_sharpe_oos=float(args.min_sharpe_oos_gate_d),
                max_abs_drawdown=float(args.max_abs_drawdown_gate_d),
                max_turnover=float(args.max_turnover_gate_d),
                max_correlation=float(args.max_correlation_gate_d),
                enable_rust_readiness_gate=bool(strict_mode),
                rust_module_name=(str(args.rust_module_name) if args.rust_module_name else None),
                rust_parity_test_path=str(args.rust_parity_test_path),
                rust_parity_timeout_s=int(args.rust_parity_timeout_s),
                enforce_rust_benchmark_gate=bool(args.enforce_rust_benchmark_gate),
                rust_benchmark_cmd=str(args.rust_benchmark_cmd),
                canary_weight=(float(args.canary_weight) if args.canary_weight is not None else None),
                force=force_promote,
                write_promotion_config=strict_mode,
            )
        )
        _write_json(promote_path, promotion.to_dict())
        promotion_out = str(promote_path)
        promotion_passed = bool(promotion.approved)

    index_rc = _run_factory_index(index_path)
    if index_rc != 0:
        notes.append("Factory index failed after validation/promotion.")

    gate_pass = bool(validation.passed) and (promotion_passed or no_promote)
    flow_pass = gate_pass and index_rc == 0
    if not validation.passed:
        notes.append("Validation did not pass Gate A/B/C.")
    if (not no_promote) and (not promotion_passed):
        notes.append("Promotion not approved (Gate D/E failed or force not enabled).")

    promotable = strict_mode and flow_pass
    if not strict_mode:
        notes.append("Triage mode result is non-promotable by governance policy.")

    final = PipelineRunReport(
        alpha_id=args.alpha_id,
        owner=args.owner,
        mode=mode,
        promotable=promotable,
        started_at=started_at,
        finished_at=_now_iso(),
        passed=flow_pass,
        factory_optimize_report=str(optimize_path),
        audit_report=str(audit_path),
        validation_report=str(validate_path),
        promotion_report=promotion_out,
        index_report=str(index_path),
        notes=notes,
    )
    _write_json(report_path, final.to_dict())
    print(f"[research.pipeline] report: {report_path}")
    print(f"[research.pipeline] mode={mode} passed={flow_pass} promotable={promotable}")

    if flow_pass or allow_gate_fail:
        return 0
    return 2


def cmd_run(args: argparse.Namespace) -> int:
    return _run_pipeline(args, mode="strict")


def cmd_triage(args: argparse.Namespace) -> int:
    if os.environ.get("HFT_RESEARCH_ALLOW_TRIAGE", "0") != "1":
        print(
            "[research.pipeline] triage is disabled by default. "
            "Set HFT_RESEARCH_ALLOW_TRIAGE=1 to acknowledge non-promotable bypass mode."
        )
        return 2
    return _run_pipeline(args, mode="triage")


def _add_common_run_args(cmd: argparse.ArgumentParser, *, strict: bool) -> None:
    cmd.add_argument(
        "--validation-profile",
        choices=(_STANDARD_VALIDATION_PROFILE, _VM_UL6_VALIDATION_PROFILE),
        default=_STANDARD_VALIDATION_PROFILE,
        help="Validation parameter profile preset. vm_ul6 enables stricter institutional-grade defaults.",
    )
    cmd.add_argument("--alpha-id", required=True, help="Alpha id under research/alphas/<alpha_id>")
    cmd.add_argument("--owner", required=True, help="Promotion owner")
    cmd.add_argument("--data", nargs="+", required=True, help="Input data paths for Gate A-C validation")
    cmd.add_argument("--project-root", default=".", help="Project root path")
    cmd.add_argument("--out-dir", default="outputs/research_pipeline", help="Output directory for pipeline reports")
    cmd.add_argument("--experiments-dir", default="research/experiments", help="Experiment base directory")
    cmd.add_argument(
        "--skip-factory-clean",
        action="store_true",
        help="Skip factory clean stage in preflight optimize.",
    )

    cmd.add_argument("--is-oos-split", type=float, default=0.7)
    cmd.add_argument("--signal-threshold", type=float, default=0.3)
    cmd.add_argument("--max-position", type=int, default=5)
    cmd.add_argument("--min-sharpe-oos-gate-c", type=float, default=0.0)
    cmd.add_argument("--max-abs-drawdown-gate-c", type=float, default=0.3)
    cmd.add_argument("--min-turnover-gate-c", type=float, default=1e-6)
    cmd.add_argument("--pytest-timeout-s", type=int, default=300)

    cmd.add_argument("--latency-profile-id", default="sim_p95_v2026-02-26")
    cmd.add_argument("--local-decision-pipeline-latency-us", type=int, default=250)
    cmd.add_argument("--submit-ack-latency-ms", type=float, default=36.0)
    cmd.add_argument("--modify-ack-latency-ms", type=float, default=43.0)
    cmd.add_argument("--cancel-ack-latency-ms", type=float, default=47.0)
    cmd.add_argument("--live-uplift-factor", type=float, default=1.5)
    cmd.add_argument("--maker-fee-bps", type=float, default=-0.2)
    cmd.add_argument("--taker-fee-bps", type=float, default=0.2)
    cmd.add_argument("--stat-pvalue-threshold", type=float, default=0.1)
    cmd.add_argument("--min-stat-tests-pass", type=int, default=2)
    cmd.add_argument("--bootstrap-samples", type=int, default=1000)
    cmd.add_argument("--opt-signal-threshold-min", type=float, default=0.05)
    cmd.add_argument("--opt-signal-threshold-max", type=float, default=0.6)
    cmd.add_argument("--opt-signal-threshold-steps", type=int, default=8)
    cmd.add_argument("--opt-objective", default="risk_adjusted")
    cmd.add_argument("--opt-max-is-oos-gap", type=float, default=1.0)
    cmd.add_argument("--opt-min-neighbor-objective-ratio", type=float, default=0.6)
    cmd.add_argument("--opt-min-deflated-sharpe", type=float, default=-0.1)
    cmd.add_argument(
        "--allowed-data-roots",
        nargs="+",
        default=[
            "research/data/raw",
            "research/data/interim",
            "research/data/processed",
            "research/data/hbt_multiproduct",
        ],
        help="Allowed dataset roots for strict data governance.",
    )
    cmd.add_argument(
        "--required-data-provenance-fields",
        nargs="*",
        default=[],
        help=(
            "Optional metadata keys required in each dataset sidecar when data governance is enforced "
            "(example: source generator seed created_at)."
        ),
    )
    cmd.add_argument(
        "--data-ul",
        type=int,
        default=2,
        help="Minimum metadata validation tier (VM-UL1..VM-UL6) used by Gate A data governance checks.",
    )
    cmd.add_argument("--stress-latency-multiplier", type=float, default=1.5)
    cmd.add_argument("--stress-fee-multiplier", type=float, default=1.5)
    cmd.add_argument("--min-stress-sharpe-ratio", type=float, default=0.5)
    cmd.add_argument("--stress-drawdown-limit-multiplier", type=float, default=1.25)

    cmd.add_argument("--shadow-sessions", type=int, default=0)
    cmd.add_argument("--min-shadow-sessions", type=int, default=5)
    cmd.add_argument("--drift-alerts", type=int, default=0)
    cmd.add_argument("--execution-reject-rate", type=float, default=0.0)
    cmd.add_argument("--max-execution-reject-rate", type=float, default=0.01)
    cmd.add_argument(
        "--paper-trade-summary",
        default=None,
        help="Optional JSON summary path for paper-trade governance (Gate E strict mode).",
    )
    cmd.add_argument("--min-paper-trade-calendar-days", type=int, default=7)
    cmd.add_argument("--min-paper-trade-trading-days", type=int, default=5)
    cmd.add_argument("--min-paper-trade-session-minutes", type=int, default=30)
    cmd.add_argument("--min-sharpe-oos-gate-d", type=float, default=1.0)
    cmd.add_argument("--max-abs-drawdown-gate-d", type=float, default=0.2)
    cmd.add_argument("--max-turnover-gate-d", type=float, default=2.0)
    cmd.add_argument("--max-correlation-gate-d", type=float, default=0.7)
    cmd.add_argument("--canary-weight", type=float, default=None)
    cmd.add_argument(
        "--rust-module-name",
        default=None,
        help="Optional rust module override used by Gate F readiness check.",
    )
    cmd.add_argument(
        "--rust-parity-test-path",
        default="tests/unit/test_rust_hotpath_parity.py",
        help="Pytest target used by Gate F Rust readiness check.",
    )
    cmd.add_argument("--rust-parity-timeout-s", type=int, default=180)
    cmd.add_argument(
        "--enforce-rust-benchmark-gate",
        action="store_true",
        help="Enable benchmark regression command in Gate F.",
    )
    cmd.add_argument(
        "--rust-benchmark-cmd",
        default=(
            "uv run python tests/benchmark/perf_regression_gate.py "
            "--baseline tests/benchmark/.benchmark_baseline.json "
            "--current benchmark.json "
            "--threshold 0.10"
        ),
    )

    if strict:
        cmd.set_defaults(func=cmd_run)
        return

    cmd.add_argument("--skip-gate-b-tests", action="store_true")
    cmd.add_argument("--no-promote", action="store_true", help="Stop after validation and skip promotion.")
    cmd.add_argument("--force-promote", action="store_true")
    cmd.add_argument(
        "--allow-audit-warnings",
        action="store_true",
        help="Do not fail pipeline when audit has warnings.",
    )
    cmd.add_argument(
        "--allow-gate-fail",
        action="store_true",
        help="Return 0 even when validation/promotion gates fail.",
    )
    cmd.set_defaults(func=cmd_triage)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single standard research workflow entrypoint (one-flow strict mode).")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Strict SOP: optimize preflight -> validate -> promote -> index (non-bypassable)")
    _add_common_run_args(run, strict=True)

    triage = sub.add_parser(
        "triage",
        help="Internal debug mode (requires HFT_RESEARCH_ALLOW_TRIAGE=1); outputs are non-promotable.",
    )
    _add_common_run_args(triage, strict=False)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
