from __future__ import annotations

import concurrent.futures
import json
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
from hft_platform.alpha.evidence import (  # noqa: E402
    EvidenceWriter,
    GovernedRunRefused,
    RunMode,
    canonical_sha256,
    require_governed,
)
from hft_platform.alpha.outcomes import Verdict  # noqa: E402
from hft_platform.alpha.provenance import file_sha256, git_state  # noqa: E402

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
# Gate C — delegate to _gate_c (fill-priced taker PnL, trend contamination check)
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
    from hft_platform.alpha.discovery import AlphaDiscoveryRegistry

    root = Path(config.project_root).resolve()
    _ensure_project_root_on_path(root)

    resolved_data_paths = [_resolve_data_path(root, path) for path in config.data_paths]
    experiments_base = _resolve_data_path(root, config.experiments_dir)
    # An unknown run_mode raises here: a typo must not run as governed or silently as exploratory.
    governed = RunMode(config.run_mode) is RunMode.GOVERNED
    writer, start_commit = _start_governed_run(config, root, Path(experiments_base)) if governed else (None, None)
    validation_dir = _make_validation_artifact_dir(Path(experiments_base), config.alpha_id)
    registry = AlphaDiscoveryRegistry()
    with _pushd(root):
        loaded = registry.discover("research/alphas")
    alpha = loaded.get(config.alpha_id)
    if alpha is None:
        known = ", ".join(sorted(loaded))
        raise ValueError(f"Unknown alpha_id '{config.alpha_id}'. Known: {known}")

    gate_a = run_gate_a(alpha.manifest, resolved_data_paths, config=config, root=root)
    _write_json(validation_dir / "feasibility_report.json", asdict(gate_a))

    gate_b = run_gate_b(
        alpha_id=config.alpha_id,
        project_root=root,
        skip_tests=config.skip_gate_b_tests,
        timeout_s=config.pytest_timeout_s,
    )
    _write_json(validation_dir / "correctness_report.json", asdict(gate_b))

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

    overall = gate_a.passed and gate_b.passed and gate_c.passed

    # Best-effort audit logging (guarded by HFT_ALPHA_AUDIT_ENABLED)
    try:
        from hft_platform.alpha.audit import log_gate_result

        for gate_report in (gate_a, gate_b, gate_c):
            log_gate_result(config.alpha_id, run_id, gate_report, cfg_hash)
    except Exception as _exc:  # noqa: BLE001
        _log.debug("audit_log_failed", alpha_id=config.alpha_id, exc_info=True)

    if writer is not None:
        _publish_governed_run(
            writer,
            config=config,
            root=root,
            validation_dir=validation_dir,
            reports={"gate_a": gate_a, "gate_b": gate_b, "gate_c": gate_c},
            run_id=run_id,
            experiment_meta_path=experiment_meta_path,
            start_commit=start_commit,
        )

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
# Governed runs: evidence root, seal, verdict ledger (ADR-005 section 6)
# ---------------------------------------------------------------------------
def _profile_name(config: ValidationConfig) -> str | None:
    return getattr(config.profile, "name", None) or None


def _start_governed_run(
    config: ValidationConfig, root: Path, experiments_base: Path
) -> tuple[EvidenceWriter, str | None]:
    """Refuse a governed run unless the tree is clean, the profile resolved and it writes to the evidence root.

    Returns the writer and the commit the run starts on; publishing requires the same commit.
    """
    commit, dirty = git_state(root)
    evidence = require_governed(git_commit=commit, git_dirty=dirty, profile_name=_profile_name(config))
    if experiments_base.resolve() != evidence:
        raise GovernedRunRefused([f"experiments_dir {experiments_base} is not the evidence root {evidence}"])
    return EvidenceWriter(evidence), commit


def _gate_c_ledger_verdict(gate_c: GateReport) -> tuple[str, str | None]:
    """The stored Gate C verdict, or INVALID with the reason it could not be read."""
    stored = gate_c.details.get("verdict")
    raw = stored.get("verdict") if isinstance(stored, dict) else None
    try:
        return Verdict(str(raw)).value, None
    except ValueError:
        return Verdict.INVALID.value, f"stored verdict unreadable: {raw!r}"


def _publish_governed_run(
    writer: EvidenceWriter,
    *,
    config: ValidationConfig,
    root: Path,
    validation_dir: Path,
    reports: dict[str, GateReport],
    run_id: str | None,
    experiment_meta_path: str | None,
    start_commit: str | None,
) -> None:
    """Seal the run's evidence and append one ledger event per gate that ran.

    The tree is checked again here: a run that dirtied it, that ends on a
    different commit than it started on, or whose Gate C stamp does not show
    that same clean commit, is refused before anything reaches the ledger.
    """
    commit, dirty = git_state(root)
    stamp: dict[str, Any] = {}
    if experiment_meta_path:
        stamp = dict(json.loads(Path(experiment_meta_path).read_text(encoding="utf-8")).get("run_stamp") or {})
    reasons = []
    if commit is None or dirty is not False:
        reasons.append("tree dirty or unknown when publishing")
    if commit != start_commit:
        reasons.append(f"HEAD moved during the run: started on {start_commit}, publishing on {commit}")
    if experiment_meta_path and (stamp.get("git_commit") != commit or stamp.get("git_dirty") is not False):
        reasons.append("Gate C RunStamp does not show the same clean commit")
    if reasons:
        raise GovernedRunRefused(reasons)

    evidence: dict[str, str] = {}
    sealed = [writer.seal(validation_dir, alpha_id=config.alpha_id, mode=RunMode.GOVERNED, extra={"run_id": run_id})]
    if experiment_meta_path:
        sealed.append(
            writer.seal(
                Path(experiment_meta_path).parent,
                alpha_id=config.alpha_id,
                mode=RunMode.GOVERNED,
                extra={"run_id": run_id, "run_stamp_sha256": canonical_sha256(stamp)},
            )
        )
    for manifest in sealed:
        evidence[writer.relpath(manifest)] = file_sha256(manifest, cached=False)

    base = {
        "alpha_id": config.alpha_id,
        "run_id": run_id or None,
        "git_commit": commit,
        "profile": _profile_name(config),
        "evidence": evidence,
    }
    for stage, report in reports.items():
        if report.details.get("skipped"):
            continue
        event: dict[str, Any] = {**base, "stage": stage, "passed": bool(report.passed)}
        if stage == "gate_c":
            event["verdict"], reason = _gate_c_ledger_verdict(report)
            if reason:
                event["verdict_reason"] = reason
        writer.append_verdict(event, mode=RunMode.GOVERNED)


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
