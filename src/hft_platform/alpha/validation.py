from __future__ import annotations

import concurrent.futures
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Re-export gate-a private helpers that tests import from validation.py
# ---------------------------------------------------------------------------
from hft_platform.alpha._gate_a import (  # noqa: E402, F401
    _FIELD_ALIASES,
    _check_hftbacktest_v2_data_format,
    _field_available,
    _load_data_fields,
    _load_paper_index,
    _resolve_paper_ref,
    _validate_dataset_metadata,
)

# ---------------------------------------------------------------------------
# Re-export _optimize_parameters so that test_alpha_validation_gates.py can
# continue importing it from validation.py.
# ---------------------------------------------------------------------------
from hft_platform.alpha._param_opt import (  # noqa: E402, F401
    _evaluate_parameter_robustness,
    _evaluate_stress_backtest,
    _optimize_parameters,
)

# ---------------------------------------------------------------------------
# Re-export stat helpers from _stat_tests (single source of truth)
# Tests and downstream code import these symbols from validation.py.
# ---------------------------------------------------------------------------
from hft_platform.alpha._stat_tests import (  # noqa: E402, F401
    _bds_correlation_delta,
    _bh_correction,
    _compute_oos_returns,
    _evaluate_oos_statistical_tests,
    _extract_bds_pvalue,
    _extract_stat_test_pvalues,
    _run_bds_independence_test,
)

# ---------------------------------------------------------------------------
# Re-export helpers from _validation_helpers (single source of truth)
# These were duplicated in validation.py; tests may import them from here.
# ---------------------------------------------------------------------------
from hft_platform.alpha._validation_helpers import (  # noqa: E402, F401
    _dataset_metadata_candidates,
    _dataset_row_count,
    _ensure_project_root_on_path,
    _has_hftbt_data,
    _load_dataset_metadata,
    _make_validation_artifact_dir,
    _missing_or_blank_metadata_keys,
    _path_under_any,
    _pushd,
    _resolve_allowed_data_roots,
    _resolve_data_path,
    _resolve_first_data_meta_path,
    _write_json,
)

# ---------------------------------------------------------------------------
# Re-export canonical types from _validation_types (single source of truth)
# ---------------------------------------------------------------------------
from hft_platform.alpha._validation_types import (  # noqa: E402
    GateReport,
    ValidationConfig,
    ValidationResult,
)

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Gate A — delegate to _gate_a (includes full data governance checks)
# ---------------------------------------------------------------------------
def run_gate_a(
    manifest: Any,
    data_paths: list[str],
    *,
    config: ValidationConfig | None = None,
    root: Path | None = None,
) -> GateReport:
    """Delegate to _gate_a.run_gate_a which includes full data governance checks."""
    from hft_platform.alpha._gate_a import run_gate_a as _run_gate_a  # noqa: PLC0415

    return _run_gate_a(manifest, data_paths, config=config, root=root)


# ---------------------------------------------------------------------------
# Gate B — already delegates to _gate_b
# ---------------------------------------------------------------------------
def run_gate_b(alpha_id: str, project_root: Path, skip_tests: bool = False, timeout_s: int = 300) -> GateReport:
    """Delegate to _gate_b.run_gate_b which includes alpha_id validation."""
    from hft_platform.alpha._gate_b import run_gate_b as _run_gate_b  # noqa: PLC0415

    return _run_gate_b(alpha_id, project_root, skip_tests=skip_tests, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Gate C — delegate to _gate_c (includes trend contamination check + sell_tax_bps)
# ---------------------------------------------------------------------------
def run_gate_c(
    alpha: Any,
    config: ValidationConfig,
    root: Path,
    resolved_data_paths: list[str],
    experiments_base: Path,
) -> tuple[GateReport, str, str, str, str]:
    """Delegate to _gate_c.run_gate_c which includes trend contamination check."""
    from hft_platform.alpha._gate_c import run_gate_c as _run_gate_c  # noqa: PLC0415

    return _run_gate_c(alpha, config, root, resolved_data_paths, experiments_base)


# ---------------------------------------------------------------------------
# Orchestrator — stays in validation.py (unique)
# ---------------------------------------------------------------------------
def run_alpha_validation(config: ValidationConfig) -> ValidationResult:
    root = Path(config.project_root).resolve()
    _ensure_project_root_on_path(root)
    from research.registry.alpha_registry import AlphaRegistry

    resolved_data_paths = [_resolve_data_path(root, path) for path in config.data_paths]
    experiments_base = _resolve_data_path(root, config.experiments_dir)
    validation_dir = _make_validation_artifact_dir(Path(experiments_base), config.alpha_id)
    registry = AlphaRegistry()
    with _pushd(root):
        loaded = registry.discover("research/alphas")
    alpha = loaded.get(config.alpha_id)
    if alpha is None:
        known = ", ".join(sorted(loaded))
        raise ValueError(f"Unknown alpha_id '{config.alpha_id}'. Known: {known}")

    gate_a = run_gate_a(alpha.manifest, resolved_data_paths, config=config, root=root)
    _write_json(validation_dir / "feasibility_report.json", asdict(gate_a))
    if gate_a.passed:
        _update_manifest_status(config.alpha_id, "GATE_A", root)

    gate_b = run_gate_b(
        alpha_id=config.alpha_id,
        project_root=root,
        skip_tests=config.skip_gate_b_tests,
        timeout_s=config.pytest_timeout_s,
    )
    _write_json(validation_dir / "correctness_report.json", asdict(gate_b))
    if gate_b.passed:
        _update_manifest_status(config.alpha_id, "GATE_B", root)

    if gate_a.passed and gate_b.passed:
        gate_c, run_id, cfg_hash, scorecard_path, experiment_meta_path = run_gate_c(
            alpha, config, root, resolved_data_paths, Path(experiments_base)
        )
    else:
        gate_c = GateReport(
            gate="Gate C",
            passed=False,
            details={
                "skipped": True,
                "reason": "Gate A or Gate B failed",
                "gate_a_passed": gate_a.passed,
                "gate_b_passed": gate_b.passed,
            },
        )
        run_id = None
        cfg_hash = None
        scorecard_path = str(validation_dir / "scorecard.json")
        _write_json(
            Path(scorecard_path),
            {
                "sharpe_is": None,
                "sharpe_oos": None,
                "ic_mean": None,
                "ic_std": None,
                "turnover": None,
                "max_drawdown": None,
                "correlation_pool_max": None,
                "regime_sharpe": {},
                "capacity_estimate": None,
                "latency_profile": {
                    "latency_profile_id": str(config.latency_profile_id),
                    "local_decision_pipeline_latency_us": int(config.local_decision_pipeline_latency_us),
                    "submit_ack_latency_ms": float(config.submit_ack_latency_ms),
                    "modify_ack_latency_ms": float(config.modify_ack_latency_ms),
                    "cancel_ack_latency_ms": float(config.cancel_ack_latency_ms),
                    "live_uplift_factor": float(config.live_uplift_factor),
                    "model_applied": False,
                    "reason": "gate_c_skipped",
                },
            },
        )
        experiment_meta_path = None

    _write_json(validation_dir / "backtest_report.json", asdict(gate_c))
    if gate_c.passed:
        _update_manifest_status(config.alpha_id, "GATE_C", root)

    overall = gate_a.passed and gate_b.passed and gate_c.passed

    # Best-effort audit logging (guarded by HFT_ALPHA_AUDIT_ENABLED)
    try:
        from hft_platform.alpha.audit import log_gate_result

        for gate_report in (gate_a, gate_b, gate_c):
            log_gate_result(config.alpha_id, run_id, gate_report, cfg_hash)
    except Exception as _exc:  # noqa: BLE001
        _log.debug("audit_log_failed", alpha_id=config.alpha_id, exc_info=True)

    return ValidationResult(
        alpha_id=config.alpha_id,
        passed=overall,
        gate_a=gate_a,
        gate_b=gate_b,
        gate_c=gate_c,
        scorecard_path=scorecard_path,
        run_id=run_id,
        config_hash=cfg_hash,
        experiment_meta_path=experiment_meta_path,
    )


# ---------------------------------------------------------------------------
# Manifest status updater — unique to validation.py
# ---------------------------------------------------------------------------
def _update_manifest_status(alpha_id: str, new_status: str, project_root: Path) -> bool:
    """Regex-replace ``status=AlphaStatus.<X>`` in an alpha's impl.py.

    Returns True if the file was updated, False if already at the target status
    or if the file does not exist.  The function is idempotent: calling it
    again with the same *new_status* is a no-op.

    Raises no exceptions — all errors are logged and False is returned.
    """
    impl_path = project_root / "research" / "alphas" / alpha_id / "impl.py"
    if not impl_path.exists():
        _log.warning(
            "alpha_status_autoupdate.impl_not_found",
            alpha_id=alpha_id,
            impl_path=str(impl_path),
        )
        return False

    try:
        original = impl_path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "alpha_status_autoupdate.read_error",
            alpha_id=alpha_id,
            error=str(exc),
        )
        return False

    pattern = r"status=AlphaStatus\.\w+"
    replacement = f"status=AlphaStatus.{new_status}"
    updated = re.sub(pattern, replacement, original)

    if updated == original:
        # Either already at target status or pattern not found — both are fine.
        _log.debug(
            "alpha_status_autoupdate.no_change",
            alpha_id=alpha_id,
            new_status=new_status,
        )
        return False

    try:
        impl_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "alpha_status_autoupdate.write_error",
            alpha_id=alpha_id,
            error=str(exc),
        )
        return False

    _log.info(
        "alpha_status_autoupdate.updated",
        alpha_id=alpha_id,
        new_status=new_status,
        impl_path=str(impl_path),
    )
    return True


# ---------------------------------------------------------------------------
# Batch validator — unique to validation.py
# ---------------------------------------------------------------------------
def batch_validate(
    *,
    alpha_ids: list[str],
    data_paths: list[str],
    gate: str = "c",
    parallel: int = 1,
    experiments_dir: str = "research/experiments",
    project_root: str = ".",
    skip_gate_b_tests: bool = False,
) -> dict[str, Any]:
    """Batch-validate multiple alphas through Gate A/B/C pipeline."""
    gate_key = str(gate).strip().lower()

    def _validate_one(alpha_id: str) -> dict[str, Any]:
        try:
            config = ValidationConfig(
                alpha_id=alpha_id,
                data_paths=list(data_paths),
                experiments_dir=experiments_dir,
                project_root=project_root,
                skip_gate_b_tests=skip_gate_b_tests or gate_key == "a",
            )
            result = run_alpha_validation(config)
            entry: dict[str, Any] = {
                "alpha_id": alpha_id,
                "passed": result.passed,
                "gate_a": result.gate_a.passed,
                "gate_b": result.gate_b.passed,
                "gate_c": result.gate_c.passed,
            }
            if gate_key == "a":
                entry["passed"] = result.gate_a.passed
            elif gate_key == "b":
                entry["passed"] = result.gate_a.passed and result.gate_b.passed
            return entry
        except Exception as exc:
            return {
                "alpha_id": alpha_id,
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    results: list[dict[str, Any]] = []
    workers = max(1, int(parallel))
    if workers > 1:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_validate_one, aid): aid for aid in alpha_ids}
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
    else:
        for alpha_id in alpha_ids:
            results.append(_validate_one(alpha_id))

    results.sort(key=lambda r: r.get("alpha_id", ""))
    passed_count = sum(1 for r in results if r.get("passed"))
    return {
        "gate": gate_key,
        "total": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "results": results,
    }
