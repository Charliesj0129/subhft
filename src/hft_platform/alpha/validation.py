from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class ValidationConfig:
    alpha_id: str
    data_paths: list[str]
    is_oos_split: float = 0.7
    signal_threshold: float = 0.3
    max_position: int = 5
    min_sharpe_oos: float = 0.0
    max_abs_drawdown: float = 0.3
    skip_gate_b_tests: bool = False
    pytest_timeout_s: int = 300
    project_root: str = "."
    experiments_dir: str = "research/experiments"


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
}


def run_alpha_validation(config: ValidationConfig) -> ValidationResult:
    from research.registry.alpha_registry import AlphaRegistry

    root = Path(config.project_root).resolve()
    resolved_data_paths = [_resolve_data_path(root, path) for path in config.data_paths]
    experiments_base = _resolve_data_path(root, config.experiments_dir)
    registry = AlphaRegistry()
    with _pushd(root):
        loaded = registry.discover("research/alphas")
    alpha = loaded.get(config.alpha_id)
    if alpha is None:
        known = ", ".join(sorted(loaded))
        raise ValueError(f"Unknown alpha_id '{config.alpha_id}'. Known: {known}")

    alpha_dir = root / "research" / "alphas" / config.alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)

    gate_a = run_gate_a(alpha.manifest, resolved_data_paths)
    _write_json(alpha_dir / "feasibility_report.json", asdict(gate_a))

    gate_b = run_gate_b(
        alpha_id=config.alpha_id,
        project_root=root,
        skip_tests=config.skip_gate_b_tests,
        timeout_s=config.pytest_timeout_s,
    )
    _write_json(alpha_dir / "correctness_report.json", asdict(gate_b))

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
        scorecard_path = str(alpha_dir / "scorecard.json")
        experiment_meta_path = None

    _write_json(alpha_dir / "backtest_report.json", asdict(gate_c))

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


def run_gate_a(manifest: Any, data_paths: list[str]) -> GateReport:
    data_fields = _load_data_fields(data_paths[0]) if data_paths else set()
    required = [str(field) for field in getattr(manifest, "data_fields", ())]
    missing = [field for field in required if not _field_available(field, data_fields)]

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

    passed = not missing and complexity_ok
    return GateReport(
        gate="Gate A",
        passed=passed,
        details={
            "missing_fields": missing,
            "available_fields": sorted(data_fields),
            "required_fields": required,
            "complexity": raw_complexity,
            "complexity_ok": complexity_ok,
            "precision_warnings": precision_warnings,
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
    from hft_platform.alpha.experiments import ExperimentTracker
    from research.backtest.hbt_runner import BacktestConfig, ResearchBacktestRunner
    from research.registry.scorecard import compute_scorecard, save_scorecard

    alpha_id = alpha.manifest.alpha_id
    alpha_dir = root / "research" / "alphas" / alpha_id
    backtest_cfg = BacktestConfig(
        data_paths=resolved_data_paths,
        is_oos_split=float(config.is_oos_split),
        signal_threshold=float(config.signal_threshold),
        max_position=int(config.max_position),
    )
    result = ResearchBacktestRunner(alpha, backtest_cfg).run()
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
        }
    )
    scorecard_path = alpha_dir / "scorecard.json"
    save_scorecard(scorecard_path, scorecard)

    passed = (result.sharpe_oos >= config.min_sharpe_oos) and (result.max_drawdown >= -abs(config.max_abs_drawdown))
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
            },
            "scorecard_path": str(scorecard_path),
        },
    )
    tracker = ExperimentTracker(base_dir=experiments_base)
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
        },
        gate_status={"gate_c": bool(passed)},
        scorecard_payload=scorecard.to_dict(),
        backtest_report_payload=asdict(report),
        signals=result.signals,
        equity=result.equity_curve,
    )
    report.details["experiment_meta_path"] = str(meta_path)
    return report, result.run_id, result.config_hash, str(scorecard_path), str(meta_path)


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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _resolve_data_path(root: Path, path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    return str(p.resolve())


@contextmanager
def _pushd(path: Path):
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)
