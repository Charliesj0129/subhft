"""Shared helpers for gate CLI commands (run-gate-c, run-gate-all).

Extracted from factory.py to reduce duplication between cmd_run_gate_c
and cmd_run_gate_all which shared ~70 lines of identical boilerplate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from structlog import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent


def load_gate_latency(profile_id: str) -> dict[str, Any]:
    """Load a latency profile by ID from versioned YAML config.

    Raises KeyError or FileNotFoundError on failure.
    """
    from research.tools.latency_profiles import load_latency_profile

    return load_latency_profile(profile_id)


def build_gate_config(
    alpha_id: str,
    data_paths: list[str],
    oos_split: float,
    latency_profile_id: str,
    latency: dict[str, Any],
    opt_threshold_min: float,
    no_opt: bool,
) -> Any:
    """Build a ValidationConfig from gate CLI arguments."""
    from src.hft_platform.alpha.validation import ValidationConfig

    return ValidationConfig(
        alpha_id=alpha_id,
        data_paths=data_paths,
        is_oos_split=oos_split,
        latency_profile_id=latency_profile_id,
        submit_ack_latency_ms=latency["submit_ack_latency_ms"],
        modify_ack_latency_ms=latency["modify_ack_latency_ms"],
        cancel_ack_latency_ms=latency["cancel_ack_latency_ms"],
        local_decision_pipeline_latency_us=latency["local_decision_pipeline_latency_us"],
        opt_signal_threshold_min=opt_threshold_min,
        enable_param_optimization=not no_opt,
    )


def discover_alpha(alpha_id: str) -> tuple[Any, Any] | None:
    """Discover an alpha by ID from the registry.

    Returns (alpha_instance, manifest) or None if not found.
    """
    from research.registry.alpha_registry import AlphaRegistry

    registry = AlphaRegistry()
    loaded = registry.discover(ROOT / "alphas")
    if alpha_id not in loaded:
        logger.error("alpha_not_found", alpha_id=alpha_id, available=sorted(loaded.keys()))
        return None

    alpha_instance = loaded[alpha_id]
    return alpha_instance, alpha_instance.manifest


def resolve_data_paths(
    paths: list[str],
    alpha_id: str,
    project_root: Path,
    owner: str,
) -> list[str]:
    """Resolve data paths relative to project root, auto-converting .jsonl files."""
    resolved: list[str] = []
    for p in paths:
        candidate = Path(p)
        if not candidate.is_absolute():
            candidate = (project_root / p).resolve()
        if candidate.suffix == ".jsonl":
            from research.tools.prepare_governed_data import prepare_clickhouse_export

            bundle = prepare_clickhouse_export(
                input_path=candidate,
                output_dir=candidate.parent / candidate.stem,
                alpha_id=alpha_id,
                owner=owner,
                split="full",
            )
            resolved.append(str(bundle.primary_data))
            logger.info("auto_convert", source=candidate.name, target=bundle.primary_data.name)
        else:
            resolved.append(str(candidate))
    return resolved


def run_gates_a_b(
    alpha_instance: Any,
    manifest: Any,
    config: Any,
    project_root: Path,
    resolved_paths: list[str],
    *,
    skip_b: bool,
    cmd_label: str,
) -> int | None:
    """Run Gate A and optionally Gate B. Returns exit code on failure, None on success."""
    from src.hft_platform.alpha.validation import run_gate_a, run_gate_b

    alpha_id = str(manifest.alpha_id)

    # --- Gate A ---
    gate_a = run_gate_a(manifest, resolved_paths, config=config, root=project_root)
    status_a = "PASS" if gate_a.passed else "FAIL"
    logger.info("gate_a_result", cmd=cmd_label, status=status_a)
    if not gate_a.passed:
        logger.info("gate_a_details", details=gate_a.details)
        return 1

    # --- Gate B ---
    if skip_b:
        logger.info("gate_b_skipped", cmd=cmd_label)
    else:
        gate_b = run_gate_b(alpha_id, project_root)
        status_b = "PASS" if gate_b.passed else "FAIL"
        logger.info("gate_b_result", cmd=cmd_label, status=status_b)
        if not gate_b.passed:
            logger.info(
                "gate_b_failure",
                stdout_tail=gate_b.details.get("stdout_tail", ""),
                stderr_tail=gate_b.details.get("stderr_tail", ""),
            )
            return 1

    return None  # success


def format_gate_c_diagnostics(
    details: dict[str, Any],
    resolved_paths: list[str],
    diagnostic_out: str | None,
) -> None:
    """Format and output Gate C diagnostic information."""
    sharpe_oos = details.get("sharpe_oos")
    sharpe_is = details.get("sharpe_is")
    ic = details.get("ic_mean")
    wf = details.get("walk_forward_consistency_pct")
    regime_sharpe = details.get("regime_sharpe", {})

    logger.info(
        "gate_c_metrics",
        sharpe_is=sharpe_is,
        sharpe_oos=sharpe_oos,
        ic_mean=ic,
        wf_consistency=wf,
    )
    if regime_sharpe:
        logger.info("regime_sharpe", **regime_sharpe)

    # --- Diagnostic output ---
    diagnostic = details.get("diagnostic", {})

    # Check for local_ts presence in data
    for dp in resolved_paths:
        try:
            arr = np.load(dp, allow_pickle=False)
            if hasattr(arr, "dtype") and arr.dtype.names:
                if "local_ts" not in arr.dtype.names:
                    logger.warning(
                        "missing_local_ts",
                        data_path=dp,
                        hint="latency model will default to 1ms/tick estimate",
                    )
        except Exception:
            pass

    if diagnostic_out and diagnostic:
        out_path = Path(diagnostic_out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(diagnostic, indent=2, sort_keys=True))
        logger.info("diagnostic_written", path=str(out_path))

    # Print diagnostic table
    diag_checks = diagnostic.get("checks", {})
    if diag_checks:
        header = f"  {'Check':<20}| {'Passed':^8}| {'Actual':<14}| {'Threshold':<14}"
        sep = f"  {'─' * 20}|{'─' * 10}|{'─' * 15}|{'─' * 14}"
        print(f"\n{header}")
        print(sep)
        for check_name, check_info in diag_checks.items():
            p_label = "PASS" if check_info["passed"] else "FAIL"
            actual = check_info.get("actual")
            threshold = check_info.get("threshold")
            actual_str = (
                "None" if actual is None
                else f"{actual:.4f}" if isinstance(actual, float) else str(actual)
            )
            threshold_str = (
                "None" if threshold is None
                else f"{threshold:.4f}" if isinstance(threshold, float) else str(threshold)
            )
            print(f"  {check_name:<20}| {p_label:^8}| {actual_str:<14}| {threshold_str:<14}")

    first_blocking = diagnostic.get("first_blocking_check")
    if first_blocking:
        logger.info("first_blocking_check", check=first_blocking)
