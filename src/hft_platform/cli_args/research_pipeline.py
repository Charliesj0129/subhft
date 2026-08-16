"""Argument contract shared by the platform and research pipeline CLIs."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

STANDARD_VALIDATION_PROFILE = "standard"
VM_UL6_VALIDATION_PROFILE = "vm_ul6"
VM_UL6_STRICT_VALIDATION_PROFILE = "vm_ul6_strict"


def add_common_research_pipeline_args(
    cmd: argparse.ArgumentParser,
    *,
    strict: bool,
    handler: Callable[[argparse.Namespace], Any],
) -> None:
    """Register the canonical research-pipeline arguments and handler."""
    cmd.add_argument(
        "--validation-profile",
        choices=(
            STANDARD_VALIDATION_PROFILE,
            VM_UL6_STRICT_VALIDATION_PROFILE,
            VM_UL6_VALIDATION_PROFILE,
        ),
        default=STANDARD_VALIDATION_PROFILE,
        help=(
            "Validation parameter profile preset. vm_ul6_strict enables stricter "
            "institutional-grade defaults loaded from "
            "config/research/profiles/vm_ul6_strict.yaml. vm_ul6 is a deprecated alias."
        ),
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

    if not strict:
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

    cmd.set_defaults(func=handler)
