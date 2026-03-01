from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class ValidationConfig:
    alpha_id: str
    data_paths: list[str]
    is_oos_split: float = 0.7
    signal_threshold: float = 0.3
    max_position: int = 5
    min_sharpe_oos: float = 0.0
    max_abs_drawdown: float = 0.3
    min_turnover: float = 1e-6
    skip_gate_b_tests: bool = False
    pytest_timeout_s: int = 300
    project_root: str = "."
    experiments_dir: str = "research/experiments"
    latency_profile_id: str = "sim_p95_v2026-02-26"
    local_decision_pipeline_latency_us: int = 250
    submit_ack_latency_ms: float = 36.0
    modify_ack_latency_ms: float = 43.0
    cancel_ack_latency_ms: float = 47.0
    live_uplift_factor: float = 1.5
    maker_fee_bps: float = -0.2
    taker_fee_bps: float = 0.2
    stat_pvalue_threshold: float = 0.1
    min_stat_tests_pass: int = 2
    stat_correction_method: str = "bh"
    min_stat_tests_bh_pass: int = 1
    enable_walk_forward: bool = True
    wf_n_splits: int = 5
    wf_min_fold_consistency: float = 0.6
    wf_min_fold_sharpe_min: float = -0.5
    enable_param_optimization: bool = True
    opt_signal_threshold_min: float = 0.05
    opt_signal_threshold_max: float = 0.6
    opt_signal_threshold_steps: int = 8
    opt_objective: str = "risk_adjusted"
    opt_max_is_oos_gap: float = 1.0
    opt_min_neighbor_objective_ratio: float = 0.6
    opt_min_deflated_sharpe: float = -0.1
    require_paper_refs: bool = False
    require_paper_index_link: bool = False
    enforce_data_governance: bool = False
    require_data_meta: bool = False
    allowed_data_roots: tuple[str, ...] = (
        "research/data/raw",
        "research/data/interim",
        "research/data/processed",
        "research/data/hbt_multiproduct",
    )
    bootstrap_samples: int = 1000
    stress_latency_multiplier: float = 1.5
    stress_fee_multiplier: float = 1.5
    min_stress_sharpe_ratio: float = 0.5
    stress_drawdown_limit_multiplier: float = 1.25


@dataclass(frozen=True)
class GateReport:
    gate: str
    passed: bool
    details: dict[str, Any]


@dataclass(frozen=True)
class ValidationResult:
    alpha_id: str
    passed: bool
    gate_a: GateReport
    gate_b: GateReport
    gate_c: GateReport
    scorecard_path: str
    run_id: str | None
    config_hash: str | None
    experiment_meta_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "bid_px": ("best_bid", "bid_price", "bid"),
    "ask_px": ("best_ask", "ask_price", "ask"),
    "bid_qty": ("bid_depth", "bid_size", "bqty"),
    "ask_qty": ("ask_depth", "ask_size", "aqty"),
    "trade_vol": ("qty", "volume", "trade_qty"),
    "current_mid": ("mid", "mid_price", "price", "close"),
    "bids": ("lob_bids", "bid_levels", "bid_book"),
    "asks": ("lob_asks", "ask_levels", "ask_book"),
}


def _bh_correction(pvalues: list[float], alpha: float) -> tuple[list[bool], list[float]]:
    """Benjamini-Hochberg FDR correction."""
    m = len(pvalues)
    if m == 0:
        return [], []

    arr = np.asarray(pvalues, dtype=np.float64)
    sort_idx = np.argsort(arr)
    sorted_p = arr[sort_idx]
    thresholds = (np.arange(1, m + 1, dtype=np.float64) / float(m)) * float(alpha)

    reject_mask = np.zeros(m, dtype=bool)
    for k in range(m - 1, -1, -1):
        if sorted_p[k] <= thresholds[k]:
            reject_mask[sort_idx[: k + 1]] = True
            break

    adjusted = np.empty(m, dtype=np.float64)
    prev = 1.0
    for k in range(m - 1, -1, -1):
        adj = float(sorted_p[k] * float(m) / float(k + 1))
        prev = min(prev, adj, 1.0)
        adjusted[sort_idx[k]] = prev

    return reject_mask.tolist(), adjusted.tolist()


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
    except Exception:
        pass  # audit must never break the research pipeline

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


def run_gate_a(
    manifest: Any,
    data_paths: list[str],
    *,
    config: ValidationConfig | None = None,
    root: Path | None = None,
) -> GateReport:
    required = [str(field) for field in getattr(manifest, "data_fields", ())]
    available_fields_union: set[str] = set()
    missing_fields_by_path: dict[str, list[str]] = {}

    for path in data_paths:
        data_fields = _load_data_fields(path)
        available_fields_union.update(data_fields)
        missing = [field for field in required if not _field_available(field, data_fields)]
        if missing:
            missing_fields_by_path[path] = missing

    if not data_paths and required:
        missing_fields_by_path["<no_data_paths>"] = list(required)

    missing_fields = sorted({field for fields in missing_fields_by_path.values() for field in fields})

    raw_complexity = str(getattr(manifest, "complexity", ""))
    complexity = raw_complexity.replace(" ", "").upper()
    complexity_ok = complexity in {"O(1)", "O(N)", "ON", "O1"}

    precision_warnings: list[str] = []
    for field in required:
        lower = field.lower()
        if "price" in lower and all(tag not in lower for tag in ("diff", "delta", "return", "spread", "mid")):
            precision_warnings.append(
                f"Field '{field}' may be raw price; ensure scaled-int processing in runtime path."
            )

    alpha_id = str(getattr(manifest, "alpha_id", "")).strip()
    paper_refs = [str(ref).strip() for ref in getattr(manifest, "paper_refs", ()) if str(ref).strip()]
    require_paper_refs = bool(config.require_paper_refs) if config is not None else False
    require_paper_index_link = bool(config.require_paper_index_link) if config is not None else False
    paper_ref_missing = bool(require_paper_refs and not paper_refs)
    unresolved_paper_refs: list[str] = []
    unmapped_paper_refs: list[str] = []
    if require_paper_index_link and paper_refs:
        paper_index = _load_paper_index(root)
        for ref in paper_refs:
            resolved_ref, row = _resolve_paper_ref(ref, paper_index)
            if resolved_ref is None or row is None:
                unresolved_paper_refs.append(ref)
                continue
            mapped = row.get("alphas") if isinstance(row, dict) else None
            mapped_set = {str(x) for x in mapped} if isinstance(mapped, (list, tuple)) else set()
            if alpha_id and alpha_id not in mapped_set:
                unmapped_paper_refs.append(ref)
    paper_governance_passed = not paper_ref_missing and not unresolved_paper_refs and not unmapped_paper_refs

    enforce_data_governance = bool(config.enforce_data_governance) if config is not None else False
    require_data_meta = bool(config.require_data_meta) if config is not None else False
    invalid_data_roots: list[str] = []
    missing_data_metadata: dict[str, str] = {}
    invalid_data_metadata: dict[str, list[str]] = {}
    allowed_roots: list[str] = []
    if enforce_data_governance:
        allowed_roots = _resolve_allowed_data_roots(root, config)
        for path_str in data_paths:
            data_path = Path(path_str).resolve()
            if allowed_roots and not _path_under_any(data_path, [Path(p) for p in allowed_roots]):
                invalid_data_roots.append(str(data_path))
            if require_data_meta:
                meta_payload, meta_path, meta_error = _load_dataset_metadata(data_path)
                if meta_payload is None:
                    reason = meta_error or "missing"
                    if meta_path is not None:
                        reason = f"{reason} ({meta_path})"
                    missing_data_metadata[str(data_path)] = reason
                    continue
                problems = _validate_dataset_metadata(meta_payload, data_path)
                if problems:
                    invalid_data_metadata[str(data_path)] = problems
    data_governance_passed = (
        (not enforce_data_governance)
        or (
            not invalid_data_roots
            and (
                not require_data_meta
                or (not missing_data_metadata and not invalid_data_metadata)
            )
        )
    )

    # Skills / roles attribution governance (warn-only, non-blocking).
    roles_used = list(str(r) for r in getattr(manifest, "roles_used", ()))
    skills_used = list(str(s) for s in getattr(manifest, "skills_used", ()))
    skills_warnings: list[str] = []
    if not skills_used:
        skills_warnings.append(
            "manifest.skills_used is empty — add skill attribution per SOP Stage 2"
            " (e.g. iterative-retrieval, hft-backtester)"
        )
    if not roles_used:
        skills_warnings.append(
            "manifest.roles_used is empty — add role attribution per SOP Stage 2"
            " (e.g. planner, code-reviewer)"
        )
    try:
        from research.registry.schemas import VALID_ROLES, VALID_SKILLS
        invalid_roles_list = [r for r in roles_used if r not in VALID_ROLES]
        invalid_skills_list = [s for s in skills_used if s not in VALID_SKILLS]
    except ImportError:
        invalid_roles_list = []
        invalid_skills_list = []
    if invalid_roles_list:
        skills_warnings.append(
            f"manifest.roles_used contains unknown values: {invalid_roles_list} "
            f"(valid roles are defined in research.registry.schemas.VALID_ROLES)"
        )
    if invalid_skills_list:
        skills_warnings.append(
            f"manifest.skills_used contains unknown values: {invalid_skills_list} "
            f"(valid skills are defined in research.registry.schemas.VALID_SKILLS)"
        )

    passed = not missing_fields_by_path and complexity_ok and paper_governance_passed and data_governance_passed
    return GateReport(
        gate="Gate A",
        passed=passed,
        details={
            "missing_fields": missing_fields,
            "missing_fields_by_path": missing_fields_by_path,
            "available_fields": sorted(available_fields_union),
            "checked_data_paths": list(data_paths),
            "required_fields": required,
            "complexity": raw_complexity,
            "complexity_ok": complexity_ok,
            "precision_warnings": precision_warnings,
            "paper_refs": paper_refs,
            "paper_governance": {
                "require_paper_refs": require_paper_refs,
                "require_paper_index_link": require_paper_index_link,
                "paper_ref_missing": paper_ref_missing,
                "unresolved_paper_refs": unresolved_paper_refs,
                "unmapped_paper_refs": unmapped_paper_refs,
                "passed": paper_governance_passed,
            },
            "data_governance": {
                "enforced": enforce_data_governance,
                "require_data_meta": require_data_meta,
                "allowed_data_roots": allowed_roots,
                "invalid_data_roots": invalid_data_roots,
                "missing_data_metadata": missing_data_metadata,
                "invalid_data_metadata": invalid_data_metadata,
                "passed": data_governance_passed,
            },
            "skills_governance": {
                "roles_used": roles_used,
                "skills_used": skills_used,
                "invalid_roles": invalid_roles_list,
                "invalid_skills": invalid_skills_list,
                "warnings": skills_warnings,
            },
        },
    )


def run_gate_b(alpha_id: str, project_root: Path, skip_tests: bool = False, timeout_s: int = 300) -> GateReport:
    if skip_tests:
        return GateReport(
            gate="Gate B",
            passed=True,
            details={"skipped": True, "reason": "skip_gate_b_tests=true"},
        )

    test_path = project_root / "research" / "alphas" / alpha_id / "tests"
    cmd = ["uv", "run", "pytest", "-q", "--no-cov", str(test_path)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        passed = proc.returncode == 0
        return GateReport(
            gate="Gate B",
            passed=passed,
            details={
                "command": " ".join(cmd),
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-4000:],
                "stderr_tail": proc.stderr[-2000:],
            },
        )
    except subprocess.TimeoutExpired as exc:
        return GateReport(
            gate="Gate B",
            passed=False,
            details={
                "command": " ".join(cmd),
                "error": f"timeout after {timeout_s}s",
                "stdout_tail": (exc.stdout or "")[-4000:],
            },
        )


def run_gate_c(
    alpha: Any,
    config: ValidationConfig,
    root: Path,
    resolved_data_paths: list[str],
    experiments_base: Path,
) -> tuple[GateReport, str, str, str, str]:
    _ensure_project_root_on_path(root)
    from hft_platform.alpha.experiments import ExperimentTracker
    from research.backtest.hbt_runner import BacktestConfig, ResearchBacktestRunner, WalkForwardConfig
    from research.registry.scorecard import compute_scorecard

    alpha_id = alpha.manifest.alpha_id
    backtest_cfg = BacktestConfig(
        data_paths=resolved_data_paths,
        is_oos_split=float(config.is_oos_split),
        signal_threshold=float(config.signal_threshold),
        max_position=int(config.max_position),
        maker_fee_bps=float(config.maker_fee_bps),
        taker_fee_bps=float(config.taker_fee_bps),
        latency_profile_id=str(config.latency_profile_id),
        local_decision_pipeline_latency_us=int(config.local_decision_pipeline_latency_us),
        submit_ack_latency_ms=float(config.submit_ack_latency_ms),
        modify_ack_latency_ms=float(config.modify_ack_latency_ms),
        cancel_ack_latency_ms=float(config.cancel_ack_latency_ms),
        live_uplift_factor=float(config.live_uplift_factor),
    )
    runner = ResearchBacktestRunner(alpha, backtest_cfg)
    base_result = runner.run()
    optimization_eval = _optimize_parameters(
        alpha=alpha,
        base_cfg=backtest_cfg,
        base_result=base_result,
        config=config,
        runner_cls=ResearchBacktestRunner,
    )
    optimization_gate_passed = bool(optimization_eval.get("passed", True))

    selected_cfg = backtest_cfg
    selected_threshold = optimization_eval.get("selected_signal_threshold")
    if selected_threshold is not None:
        try:
            threshold_val = float(selected_threshold)
            if np.isfinite(threshold_val):
                threshold_val = max(1e-6, threshold_val)
                selected_cfg = replace(backtest_cfg, signal_threshold=threshold_val)
        except (TypeError, ValueError):
            selected_cfg = backtest_cfg

    if selected_cfg.signal_threshold == backtest_cfg.signal_threshold:
        result = base_result
    else:
        runner = ResearchBacktestRunner(alpha, selected_cfg)
        result = runner.run()

    oos_returns = _compute_oos_returns(result.equity_curve, config.is_oos_split)
    stat_tests = _evaluate_oos_statistical_tests(
        oos_returns,
        pvalue_threshold=float(config.stat_pvalue_threshold),
        min_tests_pass=int(config.min_stat_tests_pass),
        bootstrap_samples=int(config.bootstrap_samples),
    )
    raw_pvalues = _extract_stat_test_pvalues(stat_tests)
    correction_method = str(config.stat_correction_method).strip().lower()
    n_tests = len(raw_pvalues)
    if correction_method == "bh":
        bh_rejected, bh_adj_pvals = _bh_correction(raw_pvalues, float(config.stat_pvalue_threshold))
    elif correction_method == "bonferroni":
        bonf_alpha = float(config.stat_pvalue_threshold) / max(1, n_tests)
        bh_rejected = [float(p) <= bonf_alpha for p in raw_pvalues]
        bh_adj_pvals = [min(float(p) * max(1, n_tests), 1.0) for p in raw_pvalues]
    else:
        correction_method = "none"
        bh_rejected = [float(p) <= float(config.stat_pvalue_threshold) for p in raw_pvalues]
        bh_adj_pvals = [float(p) for p in raw_pvalues]
    n_bh_survived = int(sum(1 for flag in bh_rejected if flag))
    required_bh_pass = int(config.min_stat_tests_bh_pass)
    if correction_method == "none":
        required_bh_pass = max(required_bh_pass, int(config.min_stat_tests_pass))
    stat_gate_passed = n_bh_survived >= required_bh_pass

    wf_result: Any | None = None
    wf_gate_passed = True
    if bool(config.enable_walk_forward):
        wf_cfg = WalkForwardConfig(n_splits=int(config.wf_n_splits))
        wf_result = runner.run_walk_forward(alpha, wf_cfg)
        wf_gate_passed = bool(
            np.isfinite(float(wf_result.fold_consistency_pct))
            and np.isfinite(float(wf_result.fold_sharpe_min))
            and float(wf_result.fold_consistency_pct) >= float(config.wf_min_fold_consistency)
            and float(wf_result.fold_sharpe_min) >= float(config.wf_min_fold_sharpe_min)
        )

    stress_eval = _evaluate_stress_backtest(
        alpha=alpha,
        base_cfg=selected_cfg,
        base_result=result,
        config=config,
        runner_cls=ResearchBacktestRunner,
    )
    robustness_eval = _evaluate_parameter_robustness(
        alpha=alpha,
        base_cfg=selected_cfg,
        base_result=result,
        runner_cls=ResearchBacktestRunner,
    )
    scorecard_extra = {
        "walk_forward_sharpe_mean": (
            float(wf_result.fold_sharpe_mean) if wf_result is not None else None
        ),
        "walk_forward_sharpe_std": (
            float(wf_result.fold_sharpe_std) if wf_result is not None else None
        ),
        "walk_forward_sharpe_min": (
            float(wf_result.fold_sharpe_min) if wf_result is not None else None
        ),
        "walk_forward_consistency_pct": (
            float(wf_result.fold_consistency_pct) if wf_result is not None else None
        ),
        "stat_bh_n_survived": int(n_bh_survived),
        "stat_bh_method": correction_method,
        "stat_bds_pvalue": _extract_bds_pvalue(stat_tests),
    }
    tracker = ExperimentTracker(base_dir=experiments_base)
    latest_signals = getattr(tracker, "latest_signals_by_alpha", None)
    pool_signals = latest_signals() if callable(latest_signals) else {}
    pool_signals = {k: v for k, v in dict(pool_signals).items() if str(k) != str(alpha_id)}
    scorecard = compute_scorecard(
        {
            "signals": result.signals,
            "sharpe_is": result.sharpe_is,
            "sharpe_oos": result.sharpe_oos,
            "ic_mean": result.ic_mean,
            "ic_std": result.ic_std,
            "turnover": result.turnover,
            "max_drawdown": result.max_drawdown,
            "regime_metrics": result.regime_metrics,
            "capacity_estimate": result.capacity_estimate,
            "latency_profile": result.latency_profile,
        },
        pool_signals=pool_signals,
        wf_extra=scorecard_extra,
    )
    scorecard_path = experiments_base / "runs" / result.run_id / "scorecard.json"

    core_passed = (
        (result.sharpe_oos >= config.min_sharpe_oos)
        and (result.max_drawdown >= -abs(config.max_abs_drawdown))
        and (result.turnover >= config.min_turnover)
    )
    passed = (
        core_passed
        and bool(stat_gate_passed)
        and bool(wf_gate_passed)
        and bool(optimization_gate_passed)
        and bool(stress_eval.get("passed"))
        and bool(robustness_eval.get("passed"))
    )
    report = GateReport(
        gate="Gate C",
        passed=passed,
        details={
            "run_id": result.run_id,
            "config_hash": result.config_hash,
            "sharpe_is": result.sharpe_is,
            "sharpe_oos": result.sharpe_oos,
            "ic_mean": result.ic_mean,
            "ic_std": result.ic_std,
            "turnover": result.turnover,
            "max_drawdown": result.max_drawdown,
            "capacity_estimate": result.capacity_estimate,
            "regime_metrics": result.regime_metrics,
            "criteria": {
                "min_sharpe_oos": config.min_sharpe_oos,
                "max_abs_drawdown": config.max_abs_drawdown,
                "min_turnover": config.min_turnover,
                "stat_pvalue_threshold": config.stat_pvalue_threshold,
                "min_stat_tests_pass": config.min_stat_tests_pass,
                "stat_correction_method": correction_method,
                "min_stat_tests_bh_pass": required_bh_pass,
                "enable_walk_forward": bool(config.enable_walk_forward),
                "wf_n_splits": int(config.wf_n_splits),
                "wf_min_fold_consistency": float(config.wf_min_fold_consistency),
                "wf_min_fold_sharpe_min": float(config.wf_min_fold_sharpe_min),
                "enable_param_optimization": bool(config.enable_param_optimization),
                "opt_signal_threshold_min": float(config.opt_signal_threshold_min),
                "opt_signal_threshold_max": float(config.opt_signal_threshold_max),
                "opt_signal_threshold_steps": int(config.opt_signal_threshold_steps),
                "opt_objective": str(config.opt_objective),
                "min_stress_sharpe_ratio": config.min_stress_sharpe_ratio,
                "stress_drawdown_limit_multiplier": config.stress_drawdown_limit_multiplier,
            },
            "core_metrics_passed": core_passed,
            "stat_gate_passed": stat_gate_passed,
            "walk_forward_gate_passed": wf_gate_passed,
            "optimization_gate_passed": optimization_gate_passed,
            "statistical_tests": stat_tests,
            "multiple_testing": {
                "method": correction_method,
                "raw_pvalues": raw_pvalues,
                "adjusted_pvalues": bh_adj_pvals,
                "rejected": bh_rejected,
                "n_survived": n_bh_survived,
                "required": required_bh_pass,
            },
            "walk_forward": (
                {
                    "n_splits": int(wf_result.config.n_splits),
                    "n_folds": len(wf_result.folds),
                    "fold_consistency_pct": float(wf_result.fold_consistency_pct),
                    "fold_sharpe_mean": float(wf_result.fold_sharpe_mean),
                    "fold_sharpe_std": float(wf_result.fold_sharpe_std),
                    "fold_sharpe_min": float(wf_result.fold_sharpe_min),
                    "fold_sharpe_max": float(wf_result.fold_sharpe_max),
                    "fold_ic_mean": float(wf_result.fold_ic_mean),
                }
                if wf_result is not None
                else {"skipped": True, "reason": "enable_walk_forward=false"}
            ),
            "parameter_optimization": optimization_eval,
            "stress_backtest": stress_eval,
            "parameter_robustness": robustness_eval,
            "latency_profile": result.latency_profile,
            "scorecard_path": str(scorecard_path),
            "selected_signal_threshold": float(selected_cfg.signal_threshold),
            "base_signal_threshold": float(backtest_cfg.signal_threshold),
        },
    )
    meta_path = tracker.log_run(
        run_id=result.run_id,
        alpha_id=alpha_id,
        config_hash=result.config_hash,
        data_paths=resolved_data_paths,
        metrics={
            "sharpe_is": float(result.sharpe_is),
            "sharpe_oos": float(result.sharpe_oos),
            "ic_mean": float(result.ic_mean),
            "ic_std": float(result.ic_std),
            "turnover": float(result.turnover),
            "max_drawdown": float(result.max_drawdown),
            "capacity_estimate": float(result.capacity_estimate),
            "latency_model_applied": float(bool(result.latency_profile.get("model_applied", False))),
            "stat_tests_passed": float(bool(stat_tests.get("passed"))),
            "stat_bh_n_survived": float(n_bh_survived),
            "walk_forward_gate_passed": float(bool(wf_gate_passed)),
            "walk_forward_consistency_pct": (
                float(wf_result.fold_consistency_pct) if wf_result is not None else float("nan")
            ),
            "param_optimization_passed": float(bool(optimization_gate_passed)),
            "selected_signal_threshold": float(selected_cfg.signal_threshold),
            "stress_test_passed": float(bool(stress_eval.get("passed"))),
            "param_robustness_passed": float(bool(robustness_eval.get("passed"))),
        },
        gate_status={"gate_c": bool(passed)},
        scorecard_payload=scorecard.to_dict(),
        backtest_report_payload=asdict(report),
        signals=result.signals,
        equity=result.equity_curve,
    )
    report.details["experiment_meta_path"] = str(meta_path)
    return report, result.run_id, result.config_hash, str(scorecard_path), str(meta_path)


def _compute_oos_returns(equity_curve: np.ndarray, is_oos_split: float) -> np.ndarray:
    eq = np.asarray(equity_curve, dtype=np.float64).reshape(-1)
    if eq.size < 3:
        return np.asarray([], dtype=np.float64)
    split = max(2, int(eq.size * float(is_oos_split)))
    split = min(split, eq.size - 1) if eq.size > 2 else eq.size
    if split >= eq.size:
        return np.asarray([], dtype=np.float64)
    segment = eq[split - 1 :]
    if segment.size < 2:
        return np.asarray([], dtype=np.float64)
    base = segment[:-1]
    delta = np.diff(segment)
    ret = np.divide(delta, base, out=np.zeros_like(delta), where=base != 0)
    return ret[np.isfinite(ret)]


def _evaluate_oos_statistical_tests(
    oos_returns: np.ndarray,
    *,
    pvalue_threshold: float,
    min_tests_pass: int,
    bootstrap_samples: int,
) -> dict[str, Any]:
    arr = np.asarray(oos_returns, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size < 20:
        return {
            "passed": False,
            "reason": "insufficient_oos_returns",
            "sample_count": int(arr.size),
            "tests_passed": 0,
            "tests_required": int(min_tests_pass),
            "pvalue_threshold": float(pvalue_threshold),
            "tests": {},
        }

    t_res = stats.ttest_1samp(arr, popmean=0.0, alternative="greater", nan_policy="omit")
    t_pvalue = float(t_res.pvalue) if np.isfinite(getattr(t_res, "pvalue", np.nan)) else 1.0
    t_pass = bool(t_pvalue <= pvalue_threshold)

    wilcoxon_pvalue = 1.0
    wilcoxon_pass = False
    nonzero = arr[arr != 0.0]
    if nonzero.size >= 10:
        try:
            w_res = stats.wilcoxon(nonzero, alternative="greater", zero_method="wilcox")
            wilcoxon_pvalue = float(w_res.pvalue) if np.isfinite(getattr(w_res, "pvalue", np.nan)) else 1.0
            wilcoxon_pass = bool(wilcoxon_pvalue <= pvalue_threshold)
        except ValueError:
            wilcoxon_pvalue = 1.0
            wilcoxon_pass = False

    sign_pvalue = 1.0
    sign_pass = False
    if nonzero.size > 0:
        pos = int(np.sum(nonzero > 0.0))
        sign_pvalue = float(stats.binomtest(pos, int(nonzero.size), p=0.5, alternative="greater").pvalue)
        sign_pass = bool(sign_pvalue <= pvalue_threshold)

    rng = np.random.default_rng(42)
    draws = max(100, int(bootstrap_samples))
    boot_means = np.empty(draws, dtype=np.float64)
    for i in range(draws):
        sample_idx = rng.integers(0, arr.size, size=arr.size)
        boot_means[i] = float(np.mean(arr[sample_idx]))
    ci_low = float(np.quantile(boot_means, 0.05))
    ci_high = float(np.quantile(boot_means, 0.95))
    bootstrap_pvalue = float(np.mean(boot_means <= 0.0))
    bootstrap_pass = bool(ci_low > 0.0)
    bds_test = _run_bds_independence_test(arr=arr, pvalue_threshold=float(pvalue_threshold))

    tests = {
        "ttest_mean_gt_zero": {"pvalue": t_pvalue, "pass": t_pass},
        "wilcoxon_gt_zero": {"pvalue": wilcoxon_pvalue, "pass": wilcoxon_pass},
        "sign_test_gt_half": {"pvalue": sign_pvalue, "pass": sign_pass},
        "bootstrap_ci_mean": {
            "pvalue": bootstrap_pvalue,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "pass": bootstrap_pass,
        },
        "bds_independence": bds_test,
    }
    signal_test_keys = ("ttest_mean_gt_zero", "wilcoxon_gt_zero", "sign_test_gt_half", "bootstrap_ci_mean")
    pass_count = int(sum(1 for key in signal_test_keys if bool(dict(tests.get(key, {})).get("pass"))))
    # BDS is a diagnostic indicator for look-ahead contamination, not a hard gate.
    # EMA-smoothed signals are always non-IID (by construction), so `pass=False` from
    # BDS does not indicate look-ahead bias. Only the four signal quality tests above
    # determine the gate result.
    diagnostic_gate_passed = bool(dict(tests.get("bds_independence", {})).get("pass", True))
    passed = pass_count >= int(min_tests_pass)
    return {
        "passed": passed,
        "sample_count": int(arr.size),
        "tests_passed": pass_count,
        "tests_required": int(min_tests_pass),
        "diagnostic_gate_passed": bool(diagnostic_gate_passed),
        "pvalue_threshold": float(pvalue_threshold),
        "mean_return": float(np.mean(arr)),
        "std_return": float(np.std(arr)),
        "tests": tests,
    }


def _extract_stat_test_pvalues(stat_tests: dict[str, Any]) -> list[float]:
    tests = stat_tests.get("tests")
    if not isinstance(tests, dict):
        return []
    out: list[float] = []
    for key in ("ttest_mean_gt_zero", "wilcoxon_gt_zero", "sign_test_gt_half", "bootstrap_ci_mean"):
        row = tests.get(key)
        if not isinstance(row, dict):
            out.append(1.0)
            continue
        try:
            p = float(row.get("pvalue", 1.0))
        except (TypeError, ValueError):
            p = 1.0
        if not np.isfinite(p):
            p = 1.0
        out.append(p)
    return out


def _extract_bds_pvalue(stat_tests: dict[str, Any]) -> float | None:
    tests = stat_tests.get("tests")
    if not isinstance(tests, dict):
        return None
    bds = tests.get("bds_independence")
    if not isinstance(bds, dict):
        return None
    try:
        p = float(bds.get("pvalue", 1.0))
        return p if np.isfinite(p) else None
    except (TypeError, ValueError):
        return None


def _run_bds_independence_test(*, arr: np.ndarray, pvalue_threshold: float) -> dict[str, Any]:
    sample = np.asarray(arr, dtype=np.float64).reshape(-1)
    sample = sample[np.isfinite(sample)]
    if sample.size < 50:
        return {
            "method": "bds",
            "available": False,
            "reason": "insufficient_samples",
            "sample_count": int(sample.size),
            "pvalue": 1.0,
            "pass": True,
        }

    max_sample = 600
    if sample.size > max_sample:
        idx = np.linspace(0, sample.size - 1, num=max_sample, dtype=np.int64)
        sample = sample[idx]

    sigma = float(np.std(sample))
    if not np.isfinite(sigma) or sigma <= 1e-12:
        return {
            "method": "bds",
            "available": False,
            "reason": "constant_series",
            "sample_count": int(sample.size),
            "pvalue": 1.0,
            "pass": True,
        }
    epsilon = float(0.7 * sigma)

    try:
        try:
            from statsmodels.tsa.stattools import bds as sm_bds  # type: ignore[import-not-found]
        except Exception:
            from statsmodels.stats.stattools import bds as sm_bds  # type: ignore[import-not-found]

        stat, pvals = sm_bds(sample, max_dim=2, epsilon=epsilon)
        stat_arr = np.asarray(stat, dtype=np.float64).reshape(-1)
        pval_arr = np.asarray(pvals, dtype=np.float64).reshape(-1)
        pvalue = float(pval_arr[-1]) if pval_arr.size else 1.0
        statistic = float(stat_arr[-1]) if stat_arr.size else float("nan")
        reject_iid = bool(np.isfinite(pvalue) and pvalue <= float(pvalue_threshold))
        return {
            "method": "statsmodels_bds",
            "available": True,
            "sample_count": int(sample.size),
            "statistic": statistic,
            "pvalue": pvalue if np.isfinite(pvalue) else 1.0,
            "null_hypothesis": "iid",
            "reject_iid": reject_iid,
            "pass": not reject_iid,
        }
    except Exception:
        # Fallback when statsmodels is unavailable: permutation proxy on BDS-style correlation integral delta.
        rng = np.random.default_rng(42)
        draws = 200
        observed = float(_bds_correlation_delta(sample, epsilon))
        permuted = np.empty(draws, dtype=np.float64)
        for i in range(draws):
            shuffled = np.array(sample, copy=True)
            rng.shuffle(shuffled)
            permuted[i] = float(_bds_correlation_delta(shuffled, epsilon))
        pvalue = float(np.mean(np.abs(permuted) >= abs(observed)))
        reject_iid = bool(pvalue <= float(pvalue_threshold))
        return {
            "method": "bds_proxy_permutation",
            "available": True,
            "sample_count": int(sample.size),
            "draws": int(draws),
            "statistic": observed,
            "pvalue": pvalue,
            "null_hypothesis": "iid",
            "reject_iid": reject_iid,
            "pass": not reject_iid,
            "note": "statsmodels_bds_unavailable_using_proxy",
        }


def _bds_correlation_delta(arr: np.ndarray, epsilon: float) -> float:
    x = np.asarray(arr, dtype=np.float64).reshape(-1)
    n = int(x.size)
    if n < 3:
        return 0.0

    diff = np.abs(x[:, None] - x[None, :])
    np.fill_diagonal(diff, np.inf)
    c1 = float(np.count_nonzero(diff < epsilon) / max(1, n * (n - 1)))

    x0 = x[:-1]
    x1 = x[1:]
    n2 = int(x0.size)
    if n2 < 2:
        return 0.0
    d0 = np.abs(x0[:, None] - x0[None, :])
    d1 = np.abs(x1[:, None] - x1[None, :])
    joint = np.maximum(d0, d1)
    np.fill_diagonal(joint, np.inf)
    c2 = float(np.count_nonzero(joint < epsilon) / max(1, n2 * (n2 - 1)))
    return float(c2 - (c1 * c1))


def _optimize_parameters(
    *,
    alpha: Any,
    base_cfg: Any,
    base_result: Any,
    config: ValidationConfig,
    runner_cls: Any,
) -> dict[str, Any]:
    if not bool(config.enable_param_optimization):
        return {
            "enabled": False,
            "passed": True,
            "objective": str(config.opt_objective),
            "selected_signal_threshold": float(base_cfg.signal_threshold),
            "selected_row": {
                "signal_threshold": float(base_cfg.signal_threshold),
                "sharpe_is": float(base_result.sharpe_is),
                "sharpe_oos": float(base_result.sharpe_oos),
                "max_drawdown": float(base_result.max_drawdown),
                "turnover": float(base_result.turnover),
                "objective": _optimization_objective(
                    float(base_result.sharpe_oos),
                    float(base_result.max_drawdown),
                    float(base_result.turnover),
                    str(config.opt_objective),
                ),
            },
            "grid": [],
            "trials": [],
            "risks": {},
        }

    lo = max(1e-6, float(config.opt_signal_threshold_min))
    hi = max(lo, float(config.opt_signal_threshold_max))
    steps = max(2, int(config.opt_signal_threshold_steps))
    grid = np.linspace(lo, hi, num=steps, dtype=np.float64)
    base_threshold = float(base_cfg.signal_threshold)
    grid = np.unique(np.append(grid, np.asarray([base_threshold], dtype=np.float64)))
    grid.sort()

    rows: list[dict[str, Any]] = []
    for threshold in grid:
        t = float(threshold)
        if abs(t - base_threshold) < 1e-12:
            result = base_result
        else:
            cfg = replace(base_cfg, signal_threshold=t)
            result = runner_cls(alpha, cfg).run()
        objective = _optimization_objective(
            float(result.sharpe_oos),
            float(result.max_drawdown),
            float(result.turnover),
            str(config.opt_objective),
        )
        rows.append(
            {
                "signal_threshold": t,
                "sharpe_is": float(result.sharpe_is),
                "sharpe_oos": float(result.sharpe_oos),
                "max_drawdown": float(result.max_drawdown),
                "turnover": float(result.turnover),
                "objective": float(objective),
                "run_id": str(result.run_id),
                "config_hash": str(result.config_hash),
            }
        )

    if not rows:
        return {
            "enabled": True,
            "passed": False,
            "objective": str(config.opt_objective),
            "selected_signal_threshold": float(base_threshold),
            "selected_row": None,
            "grid": [],
            "trials": [],
            "risks": {"no_trials": True},
        }

    ranked = sorted(rows, key=lambda row: float(row["objective"]), reverse=True)
    best = ranked[0]
    best_threshold = float(best["signal_threshold"])
    idx = min(range(len(rows)), key=lambda i: abs(float(rows[i]["signal_threshold"]) - best_threshold))

    neighbors: list[dict[str, Any]] = []
    if idx - 1 >= 0:
        neighbors.append(rows[idx - 1])
    if idx + 1 < len(rows):
        neighbors.append(rows[idx + 1])

    best_obj = float(best["objective"])
    neighbor_objs = np.asarray([float(row["objective"]) for row in neighbors], dtype=np.float64)
    neighbor_sharpes = np.asarray([float(row["sharpe_oos"]) for row in neighbors], dtype=np.float64)

    boundary_risk = idx in {0, len(rows) - 1}
    overfit_gap = float(best["sharpe_is"]) - float(best["sharpe_oos"])
    overfit_gap_risk = overfit_gap > float(config.opt_max_is_oos_gap)

    neighbor_ratio = 1.0
    if neighbor_objs.size and best_obj != 0.0:
        neighbor_ratio = float(np.median(neighbor_objs) / best_obj)
    plateau_risk = bool(
        best_obj > 0.0
        and neighbor_objs.size
        and neighbor_ratio < float(config.opt_min_neighbor_objective_ratio)
    )
    sign_flip_risk = bool(
        float(best["sharpe_oos"]) > 0.0 and neighbor_sharpes.size and np.any(neighbor_sharpes <= 0.0)
    )

    oos_len = max(2, int((1.0 - float(config.is_oos_split)) * float(base_result.equity_curve.size)))
    n_trials = max(1, len(rows))
    selection_penalty = float(np.sqrt(2.0 * np.log(float(n_trials)) / float(oos_len)))
    deflated_sharpe = float(best["sharpe_oos"]) - selection_penalty
    selection_bias_risk = deflated_sharpe < float(config.opt_min_deflated_sharpe)

    risks = {
        "boundary_risk": bool(boundary_risk),
        "overfit_gap_risk": bool(overfit_gap_risk),
        "plateau_risk": bool(plateau_risk),
        "sign_flip_risk": bool(sign_flip_risk),
        "selection_bias_risk": bool(selection_bias_risk),
    }
    passed = not any(risks.values())
    return {
        "enabled": True,
        "passed": bool(passed),
        "objective": str(config.opt_objective),
        "selected_signal_threshold": float(best_threshold),
        "selected_row": best,
        "base_signal_threshold": float(base_threshold),
        "grid": [float(v) for v in grid],
        "trials": rows,
        "top_k": ranked[: min(3, len(ranked))],
        "deflated_sharpe": float(deflated_sharpe),
        "selection_penalty": float(selection_penalty),
        "neighbor_objective_ratio": float(neighbor_ratio),
        "risks": risks,
    }


def _optimization_objective(
    sharpe_oos: float,
    max_drawdown: float,
    turnover: float,
    objective: str,
) -> float:
    mode = objective.strip().lower()
    if mode == "sharpe_oos":
        return float(sharpe_oos)
    if mode == "ic_first":
        # Fallback objective when IC-first mode is requested but IC is unavailable in this stage.
        return float(sharpe_oos) - 0.1 * abs(float(turnover))
    # Default: risk-adjusted objective.
    drawdown_penalty = max(0.0, abs(float(max_drawdown)) - 0.10) * 2.0
    turnover_penalty = max(0.0, float(turnover) - 1.0) * 0.25
    return float(sharpe_oos) - float(drawdown_penalty) - float(turnover_penalty)


def _evaluate_stress_backtest(
    *,
    alpha: Any,
    base_cfg: Any,
    base_result: Any,
    config: ValidationConfig,
    runner_cls: Any,
) -> dict[str, Any]:
    latency_mult = max(1.0, float(config.stress_latency_multiplier))
    fee_mult = max(1.0, float(config.stress_fee_multiplier))
    stress_cfg = replace(
        base_cfg,
        maker_fee_bps=float(base_cfg.maker_fee_bps) * fee_mult,
        taker_fee_bps=float(base_cfg.taker_fee_bps) * fee_mult,
        submit_ack_latency_ms=float(base_cfg.submit_ack_latency_ms) * latency_mult,
        modify_ack_latency_ms=float(base_cfg.modify_ack_latency_ms) * latency_mult,
        cancel_ack_latency_ms=float(base_cfg.cancel_ack_latency_ms) * latency_mult,
        live_uplift_factor=float(base_cfg.live_uplift_factor) * latency_mult,
        latency_profile_id=f"{base_cfg.latency_profile_id}_stress",
    )
    stress_result = runner_cls(alpha, stress_cfg).run()

    base_sharpe = float(base_result.sharpe_oos)
    stress_sharpe = float(stress_result.sharpe_oos)
    sharpe_ratio = (stress_sharpe / base_sharpe) if abs(base_sharpe) > 1e-12 else None
    if base_sharpe > 0.0:
        sharpe_pass = bool(stress_sharpe >= (base_sharpe * float(config.min_stress_sharpe_ratio)))
    else:
        sharpe_pass = bool(stress_sharpe >= float(config.min_sharpe_oos))

    stress_dd_limit = -abs(float(config.max_abs_drawdown)) * max(1.0, float(config.stress_drawdown_limit_multiplier))
    drawdown_pass = bool(float(stress_result.max_drawdown) >= stress_dd_limit)
    passed = sharpe_pass and drawdown_pass
    return {
        "passed": passed,
        "stress_run_id": str(stress_result.run_id),
        "stress_config_hash": str(stress_result.config_hash),
        "stress_sharpe_oos": stress_sharpe,
        "base_sharpe_oos": base_sharpe,
        "stress_sharpe_ratio_vs_base": sharpe_ratio,
        "stress_max_drawdown": float(stress_result.max_drawdown),
        "stress_drawdown_limit": stress_dd_limit,
        "checks": {
            "sharpe_resilience": sharpe_pass,
            "drawdown_limit": drawdown_pass,
        },
        "multipliers": {
            "latency": latency_mult,
            "fees": fee_mult,
        },
    }


def _evaluate_parameter_robustness(
    *,
    alpha: Any,
    base_cfg: Any,
    base_result: Any,
    runner_cls: Any,
) -> dict[str, Any]:
    ratios = (0.8, 1.0, 1.2)
    rows: list[dict[str, Any]] = []
    for ratio in ratios:
        if abs(ratio - 1.0) < 1e-12:
            result = base_result
            threshold = float(base_cfg.signal_threshold)
        else:
            threshold = max(1e-6, float(base_cfg.signal_threshold) * float(ratio))
            cfg = replace(base_cfg, signal_threshold=threshold)
            result = runner_cls(alpha, cfg).run()
        rows.append(
            {
                "ratio": float(ratio),
                "signal_threshold": float(threshold),
                "sharpe_oos": float(result.sharpe_oos),
                "max_drawdown": float(result.max_drawdown),
                "turnover": float(result.turnover),
            }
        )

    base = next((row for row in rows if abs(float(row["ratio"]) - 1.0) < 1e-12), rows[0])
    neighbors = [row for row in rows if abs(float(row["ratio"]) - 1.0) > 1e-12]
    neighbor_sharpes = np.asarray([float(row["sharpe_oos"]) for row in neighbors], dtype=np.float64)
    neighbor_turnovers = np.asarray([float(row["turnover"]) for row in neighbors], dtype=np.float64)
    neighbor_drawdowns = np.asarray([float(row["max_drawdown"]) for row in neighbors], dtype=np.float64)

    base_sharpe = float(base["sharpe_oos"])
    base_turnover = float(base["turnover"])
    base_drawdown = float(base["max_drawdown"])
    median_neighbor_sharpe = float(np.median(neighbor_sharpes)) if neighbor_sharpes.size else float("nan")
    cliff_limit = max(0.25, abs(base_sharpe) * 0.6)

    cliff_risk = bool(
        base_sharpe > 0.0
        and np.isfinite(median_neighbor_sharpe)
        and (base_sharpe - median_neighbor_sharpe) > cliff_limit
    )
    sign_flip_risk = bool(base_sharpe > 0.0 and neighbor_sharpes.size and np.any(neighbor_sharpes <= 0.0))
    turnover_spike_risk = bool(
        base_turnover > 0.0
        and neighbor_turnovers.size
        and np.any(neighbor_turnovers > max(base_turnover * 2.0, base_turnover + 0.5))
    )
    drawdown_jump_risk = bool(
        neighbor_drawdowns.size
        and np.any(np.abs(neighbor_drawdowns) > max(abs(base_drawdown) * 1.5, abs(base_drawdown) + 0.05))
    )
    risks = {
        "cliff_risk": cliff_risk,
        "sign_flip_risk": sign_flip_risk,
        "turnover_spike_risk": turnover_spike_risk,
        "drawdown_jump_risk": drawdown_jump_risk,
    }
    passed = not any(risks.values())
    return {
        "passed": passed,
        "ratios": list(ratios),
        "sweep": rows,
        "median_neighbor_sharpe": median_neighbor_sharpe if np.isfinite(median_neighbor_sharpe) else None,
        "risks": risks,
    }


def _make_validation_artifact_dir(experiments_base: Path, alpha_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = experiments_base / "validations" / alpha_id / f"{stamp}_{uuid4().hex[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _load_data_fields(path: str) -> set[str]:
    source = np.load(path, allow_pickle=False)
    try:
        if isinstance(source, np.lib.npyio.NpzFile):
            if "data" not in source:
                return set()
            arr = np.asarray(source["data"])
        else:
            arr = np.asarray(source)
    finally:
        if isinstance(source, np.lib.npyio.NpzFile):
            source.close()

    if arr.dtype.names:
        return set(str(name) for name in arr.dtype.names)
    return set()


def _field_available(field: str, available: set[str]) -> bool:
    if field == "current_mid":
        if ("best_bid" in available and "best_ask" in available) or ("bid_px" in available and "ask_px" in available):
            return True
    if field in available:
        return True
    aliases = _FIELD_ALIASES.get(field, ())
    return any(alias in available for alias in aliases)


def _load_paper_index(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    index_path = root / "research" / "knowledge" / "paper_index.json"
    if not index_path.exists():
        return {}
    try:
        payload = json.loads(index_path.read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_paper_ref(ref: str, paper_index: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    if ref in paper_index and isinstance(paper_index[ref], dict):
        return ref, paper_index[ref]
    for key, row in paper_index.items():
        if not isinstance(row, dict):
            continue
        if str(row.get("arxiv_id", "")).strip() == ref:
            return str(key), row
    return None, None


def _resolve_allowed_data_roots(root: Path | None, config: ValidationConfig | None) -> list[str]:
    if root is None or config is None:
        return []
    out: list[str] = []
    for rel in tuple(config.allowed_data_roots):
        text = str(rel).strip()
        if not text:
            continue
        p = Path(text)
        if not p.is_absolute():
            p = root / p
        out.append(str(p.resolve()))
    return out


def _path_under_any(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for base in roots:
        base_resolved = base.resolve()
        if resolved == base_resolved:
            return True
        if base_resolved in resolved.parents:
            return True
    return False


def _dataset_metadata_candidates(data_path: Path) -> list[Path]:
    return [
        data_path.with_suffix(data_path.suffix + ".meta.json"),
        data_path.with_suffix(".meta.json"),
        data_path.with_suffix(data_path.suffix + ".metadata.json"),
        data_path.with_suffix(".metadata.json"),
    ]


def _load_dataset_metadata(data_path: Path) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    for meta_path in _dataset_metadata_candidates(data_path):
        if not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text())
        except (OSError, ValueError) as exc:
            return None, meta_path, f"invalid_json:{exc}"
        if not isinstance(payload, dict):
            return None, meta_path, "invalid_format"
        return payload, meta_path, None
    return None, None, "missing_meta_file"


def _validate_dataset_metadata(meta: dict[str, Any], data_path: Path) -> list[str]:
    problems: list[str] = []
    required_keys = (
        "dataset_id",
        "source_type",
        "owner",
        "schema_version",
        "rows",
        "fields",
    )
    for key in required_keys:
        if key not in meta:
            problems.append(f"missing:{key}")

    source_type = str(meta.get("source_type", "")).strip().lower()
    if source_type and source_type not in {"synthetic", "real"}:
        problems.append("source_type_must_be_synthetic_or_real")

    try:
        schema_version = int(meta.get("schema_version", 0))
        if schema_version < 1:
            problems.append("schema_version_must_be>=1")
    except (TypeError, ValueError):
        problems.append("schema_version_not_int")

    try:
        rows_meta = int(meta.get("rows", -1))
        if rows_meta <= 0:
            problems.append("rows_must_be>0")
    except (TypeError, ValueError):
        rows_meta = -1
        problems.append("rows_not_int")

    actual_rows = _dataset_row_count(data_path)
    if actual_rows is not None and rows_meta > 0 and rows_meta != actual_rows:
        problems.append(f"rows_mismatch(meta={rows_meta},actual={actual_rows})")

    fields = meta.get("fields")
    if not isinstance(fields, list) or not fields:
        problems.append("fields_must_be_nonempty_list")
    return problems


def _dataset_row_count(path: Path) -> int | None:
    source = np.load(path, allow_pickle=False)
    try:
        if isinstance(source, np.lib.npyio.NpzFile):
            if "data" in source:
                arr = np.asarray(source["data"])
            elif source.files:
                arr = np.asarray(source[source.files[0]])
            else:
                return 0
        else:
            arr = np.asarray(source)
        if arr.ndim == 0:
            return int(arr.size)
        return int(arr.shape[0])
    except Exception:
        return None
    finally:
        if isinstance(source, np.lib.npyio.NpzFile):
            source.close()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _resolve_data_path(root: Path, path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    return str(p.resolve())


def _ensure_project_root_on_path(root: Path | None = None) -> None:
    candidates = [root, Path(__file__).resolve().parents[3], Path.cwd()]
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = Path(candidate).resolve()
        if not (resolved / "research").exists():
            continue
        resolved_str = str(resolved)
        if resolved_str not in sys.path:
            sys.path.insert(0, resolved_str)


@contextmanager
def _pushd(path: Path):
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)
