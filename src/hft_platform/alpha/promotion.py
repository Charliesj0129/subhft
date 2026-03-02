"""Alpha promotion pipeline — Gate D (backtest) + Gate E (paper-trade) + Gate F (Rust readiness).

``promote_alpha`` is the single entry point.  It:
  1. Loads the backtest scorecard produced by Gate C.
  2. Evaluates Gate D (quantitative thresholds: Sharpe, drawdown, turnover, correlation).
  3. Evaluates Gate E (paper-trade governance: 1-week span, drift, rejection quality).
  4. Evaluates Gate F (Rust readiness: manifest + parity tests [+ optional perf gate]).
  5. Writes ``integration_report.json`` and ``promotion_decision.json`` under experiments promotions.
  6. On approval, writes a YAML promotion config under ``config/strategy_promotions/``.
  7. Best-effort audit logs to ClickHouse (guarded by ``HFT_ALPHA_AUDIT_ENABLED``).
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml


@dataclass(frozen=True)
class PromotionConfig:
    alpha_id: str
    owner: str
    project_root: str = "."
    experiments_dir: str = "research/experiments"
    scorecard_path: str | None = None
    shadow_sessions: int = 0
    min_shadow_sessions: int = 5
    drift_alerts: int = 0
    execution_reject_rate: float = 0.0
    max_execution_reject_rate: float = 0.01
    require_paper_trade_governance: bool = False
    paper_trade_summary_path: str | None = None
    min_paper_trade_calendar_days: int = 7
    min_paper_trade_trading_days: int = 5
    min_paper_trade_session_minutes: int = 60
    min_sharpe_oos: float = 1.0
    max_abs_drawdown: float = 0.2
    max_turnover: float = 2.0
    max_correlation: float = 0.7
    enable_rust_readiness_gate: bool = False
    rust_module_name: str | None = None
    rust_parity_test_path: str = "tests/unit/test_rust_hotpath_parity.py"
    rust_parity_timeout_s: int = 180
    enforce_rust_benchmark_gate: bool = False
    rust_benchmark_cmd: str = (
        "uv run python tests/benchmark/perf_regression_gate.py "
        "--baseline tests/benchmark/.benchmark_baseline.json "
        "--current benchmark.json "
        "--threshold 0.10"
    )
    canary_weight: float | None = None
    expiry_days: int = 30
    max_live_slippage_bps: float = 3.0
    max_live_drawdown_contribution: float = 0.02
    max_execution_error_rate: float = 0.01
    force: bool = False
    write_promotion_config: bool = True
    config_version: str = "v1"
    parent_config_version: str | None = None
    # Feature set version from the alpha manifest.  When set, Gate D warns
    # (warn-only, non-blocking) if it doesn't match the live engine version.
    manifest_feature_set_version: str | None = None


@dataclass(frozen=True)
class PromotionChecklistItem:
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PromotionChecklist:
    items: list[PromotionChecklistItem]

    def all_passed(self) -> bool:
        return all(i.passed for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_passed": self.all_passed(),
            "items": [{"label": i.label, "passed": i.passed, "detail": i.detail} for i in self.items],
        }


@dataclass(frozen=True)
class PromotionResult:
    alpha_id: str
    approved: bool
    forced: bool
    gate_d_passed: bool
    gate_e_passed: bool
    gate_f_passed: bool
    canary_weight: float
    integration_report_path: str
    promotion_decision_path: str
    promotion_config_path: str | None
    reasons: list[str]
    checklist: PromotionChecklist | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.checklist is not None:
            d["checklist"] = self.checklist.to_dict()
        return d


def promote_alpha(config: PromotionConfig) -> PromotionResult:
    root = Path(config.project_root).resolve()
    alpha_dir = root / "research" / "alphas" / config.alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)

    scorecard_path = _resolve_scorecard_path(root, config, alpha_dir)
    if not scorecard_path.exists():
        raise FileNotFoundError(f"scorecard not found: {scorecard_path}")

    scorecard = json.loads(scorecard_path.read_text())
    data_ul_value = _to_float(scorecard.get("data_ul"))
    data_ul = int(data_ul_value) if data_ul_value is not None else None
    data_ul_advisory = {
        "value": data_ul,
        "gate_d_recommended_min": 4,
        "gate_d_warn": (data_ul is None or data_ul < 4),
        "gate_e_recommended_min": 5,
        "gate_e_warn": (data_ul is None or data_ul < 5),
        "blocking": False,
    }
    gate_d_passed, gate_d_checks = _evaluate_gate_d(scorecard, config)
    gate_e_passed, gate_e_checks = _evaluate_gate_e(config, root)
    gate_f_passed, gate_f_checks = _evaluate_gate_f(config, root)

    approved = gate_d_passed and gate_e_passed and gate_f_passed
    forced = False
    reasons: list[str] = []
    if not approved:
        reasons.append("One or more promotion gates failed")
        if config.force:
            approved = True
            forced = True
            reasons.append("force=true override")

    weight = _suggest_canary_weight(scorecard, override=config.canary_weight) if approved else 0.0
    now = datetime.now(UTC)
    expiry = (now + timedelta(days=max(config.expiry_days, 1))).date().isoformat()
    artifact_dir = _promotion_artifact_dir(root, config.alpha_id, now)

    integration_report = {
        "alpha_id": config.alpha_id,
        "gate": "Gate D",
        "passed": gate_d_passed,
        "checks": gate_d_checks,
        "data_ul_advisory": data_ul_advisory,
        "scorecard_path": str(scorecard_path),
        "timestamp": now.isoformat(),
    }
    integration_report_path = artifact_dir / "integration_report.json"
    _write_json(integration_report_path, integration_report)

    decision_payload = {
        "alpha_id": config.alpha_id,
        "gate_d_passed": gate_d_passed,
        "gate_e_passed": gate_e_passed,
        "gate_f_passed": gate_f_passed,
        "gate_e_checks": gate_e_checks,
        "gate_f_checks": gate_f_checks,
        "data_ul_advisory": data_ul_advisory,
        "decision": "approve" if approved else "reject",
        "forced": forced,
        "reasons": reasons,
        "canary_weight": weight,
        "timestamp": now.isoformat(),
    }
    decision_path = artifact_dir / "promotion_decision.json"
    _write_json(decision_path, decision_payload)

    promotion_config_path: Path | None = None
    if approved and config.write_promotion_config:
        promotion_config_path = _write_promotion_config(
            root=root,
            config=config,
            canary_weight=weight,
            expiry_review_date=expiry,
            scorecard=scorecard,
            approved=approved,
            forced=forced,
        )
    elif approved and not config.write_promotion_config:
        reasons.append("promotion_config_write_disabled=true (research-only run)")

    checklist = build_promotion_checklist(config, gate_d_checks, gate_e_checks, gate_f_checks)

    result = PromotionResult(
        alpha_id=config.alpha_id,
        approved=approved,
        forced=forced,
        gate_d_passed=gate_d_passed,
        gate_e_passed=gate_e_passed,
        gate_f_passed=gate_f_passed,
        canary_weight=weight,
        integration_report_path=str(integration_report_path),
        promotion_decision_path=str(decision_path),
        promotion_config_path=str(promotion_config_path) if promotion_config_path else None,
        reasons=reasons,
        checklist=checklist,
    )

    # Best-effort audit logging (guarded by HFT_ALPHA_AUDIT_ENABLED)
    try:
        from hft_platform.alpha.audit import log_gate_result, log_promotion_result
        from hft_platform.alpha.validation import GateReport

        gate_d_report = GateReport(gate="Gate D", passed=gate_d_passed, details=gate_d_checks)
        gate_e_report = GateReport(gate="Gate E", passed=gate_e_passed, details=gate_e_checks)
        gate_f_report = GateReport(gate="Gate F", passed=gate_f_passed, details=gate_f_checks)
        cfg_hash = scorecard.get("config_hash", "")
        for gate_report in (gate_d_report, gate_e_report, gate_f_report):
            log_gate_result(config.alpha_id, None, gate_report, cfg_hash)
        log_promotion_result(result, cfg_hash, scorecard)
    except Exception:
        pass  # audit must never break the research pipeline

    return result


def _evaluate_gate_d(scorecard: dict[str, Any], config: PromotionConfig) -> tuple[bool, dict[str, Any]]:
    sharpe = _to_float(scorecard.get("sharpe_oos"))
    max_dd = _to_float(scorecard.get("max_drawdown"))
    turnover = _to_float(scorecard.get("turnover"))
    corr = _to_float(scorecard.get("correlation_pool_max"))
    latency_profile = scorecard.get("latency_profile") or None

    checks: dict[str, dict[str, Any]] = {
        "sharpe_oos": {
            "value": sharpe,
            "min": config.min_sharpe_oos,
            "pass": (sharpe is not None and sharpe >= config.min_sharpe_oos),
        },
        "max_drawdown": {
            "value": max_dd,
            "min": -abs(config.max_abs_drawdown),
            "pass": (max_dd is not None and max_dd >= -abs(config.max_abs_drawdown)),
        },
        "turnover": {
            "value": turnover,
            "max": config.max_turnover,
            "pass": (turnover is not None and turnover <= config.max_turnover),
        },
        "correlation_pool_max": {
            "value": corr,
            "max": config.max_correlation,
            "required": True,
            "pass": (corr is not None and corr <= config.max_correlation),
            "detail": (
                "OK"
                if corr is not None
                else "MISSING — scorecard.correlation_pool_max must be populated before promotion"
            ),
        },
        # Latency realism governance (CLAUDE.md constitution requirement).
        # Missing latency_profile in the scorecard = NOT promotion-ready.
        # Blocks Gate D: alpha must record P95 broker RTT assumptions before promotion.
        "latency_profile": {
            "value": latency_profile,
            "required": True,
            "pass": latency_profile is not None,
            "detail": (
                "OK"
                if latency_profile
                else "MISSING — must record P95 Shioaji broker RTT assumptions "
                "(see docs/architecture/latency-baseline-shioaji-sim-vs-system.md)"
            ),
        },
    }
    # Feature set version parity check (warn-only: does NOT block Gate D).
    manifest_fsv = str(config.manifest_feature_set_version or "").strip() or None
    _LIVE_FSV: str | None = None
    try:
        from hft_platform.feature.registry import FEATURE_SET_VERSION as _LIVE_FSV
    except Exception:
        pass
    if manifest_fsv is not None and _LIVE_FSV is not None:
        fsv_match = manifest_fsv == _LIVE_FSV
        checks["feature_set_version"] = {
            "manifest": manifest_fsv,
            "live_engine": _LIVE_FSV,
            "match": fsv_match,
            "pass": fsv_match,  # blocking: mismatch fails Gate D
            "detail": (
                "OK"
                if fsv_match
                else f"MISMATCH — manifest declares '{manifest_fsv}' but live engine uses '{_LIVE_FSV}'. "
                "Re-run backtest with the current feature set before promoting to live."
            ),
        }

    passed = all(bool(v["pass"]) for v in checks.values())
    return passed, checks


def _resolve_scorecard_path(root: Path, config: PromotionConfig, alpha_dir: Path) -> Path:
    if config.scorecard_path:
        path = Path(config.scorecard_path)
        if not path.is_absolute():
            path = root / path
        return path

    latest = _latest_scorecard_from_runs(root, config.alpha_id)
    if latest is not None:
        return latest

    # Backward-compatible fallback for legacy/manual runs.
    return alpha_dir / "scorecard.json"


def _latest_scorecard_from_runs(root: Path, alpha_id: str) -> Path | None:
    try:
        from hft_platform.alpha.experiments import ExperimentTracker

        tracker = ExperimentTracker(base_dir=root / "research" / "experiments")
        for row in tracker.list_runs(alpha_id=alpha_id):
            if not row.scorecard_path:
                continue
            scorecard = Path(row.scorecard_path)
            if scorecard.exists():
                return scorecard
    except Exception:
        return None
    return None


def _promotion_artifact_dir(root: Path, alpha_id: str, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = root / "research" / "experiments" / "promotions" / alpha_id / f"{stamp}_{uuid4().hex[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _evaluate_gate_e(config: PromotionConfig, root: Path) -> tuple[bool, dict[str, Any]]:
    paper_summary, summary_source, summary_error = _resolve_paper_trade_summary(config, root)
    summary_session_count = int(paper_summary.get("session_count", 0)) if paper_summary else 0
    summary_calendar_days = int(paper_summary.get("calendar_span_days", 0)) if paper_summary else 0
    summary_trading_days = int(paper_summary.get("distinct_trading_days", 0)) if paper_summary else 0
    summary_min_session_seconds = int(paper_summary.get("min_session_duration_seconds", 0)) if paper_summary else 0
    summary_invalid_session_durations = (
        int(paper_summary.get("invalid_session_duration_count", summary_session_count)) if paper_summary else 0
    )
    summary_drift_alerts = int(paper_summary.get("drift_alerts_total", 0)) if paper_summary else 0
    summary_reject_rate_mean = float(paper_summary.get("execution_reject_rate_mean", 0.0)) if paper_summary else 0.0
    # Prefer P95 reject rate over mean per CLAUDE.md latency realism policy.
    # Falls back to mean if P95 not recorded (legacy sessions).
    _p95_raw = paper_summary.get("execution_reject_rate_p95") if paper_summary else None
    summary_reject_rate = float(_p95_raw) if _p95_raw is not None else summary_reject_rate_mean

    shadow_sessions = summary_session_count if paper_summary else int(config.shadow_sessions)
    drift_alerts = summary_drift_alerts if paper_summary else int(config.drift_alerts)
    execution_reject_rate = summary_reject_rate if paper_summary else float(config.execution_reject_rate)

    checks: dict[str, dict[str, Any]] = {
        "shadow_sessions": {
            "value": shadow_sessions,
            "min": config.min_shadow_sessions,
            "pass": shadow_sessions >= config.min_shadow_sessions,
        },
        "drift_alerts": {"value": drift_alerts, "max": 0, "pass": drift_alerts == 0},
        "execution_reject_rate": {
            "value": execution_reject_rate,
            "max": config.max_execution_reject_rate,
            "pass": execution_reject_rate <= config.max_execution_reject_rate,
            "source": "p95" if _p95_raw is not None else "mean",
        },
    }

    mode = "manual_shadow"
    if config.require_paper_trade_governance:
        mode = "paper_trade_governed"
        has_summary = bool(paper_summary) and summary_error is None
        checks["paper_trade_log_available"] = {
            "value": has_summary,
            "required": True,
            "pass": has_summary,
            "source": summary_source,
            "error": summary_error,
        }
        checks["paper_trade_calendar_days"] = {
            "value": summary_calendar_days if has_summary else 0,
            "min": int(config.min_paper_trade_calendar_days),
            "pass": has_summary and summary_calendar_days >= int(config.min_paper_trade_calendar_days),
        }
        checks["paper_trade_trading_days"] = {
            "value": summary_trading_days if has_summary else 0,
            "min": int(config.min_paper_trade_trading_days),
            "pass": has_summary and summary_trading_days >= int(config.min_paper_trade_trading_days),
        }
        required_session_seconds = max(60, int(config.min_paper_trade_session_minutes) * 60)
        checks["paper_trade_session_duration"] = {
            "value": summary_min_session_seconds if has_summary else 0,
            "min_seconds": int(required_session_seconds),
            "min_minutes": int(config.min_paper_trade_session_minutes),
            "invalid_session_duration_count": (
                summary_invalid_session_durations if has_summary else int(shadow_sessions)
            ),
            "pass": (
                has_summary
                and summary_invalid_session_durations == 0
                and summary_min_session_seconds >= int(required_session_seconds)
            ),
        }

    # Regime-span check (warn-only: does NOT block Gate E).
    # Sessions should cover ≥2 distinct regimes for adequate test diversity.
    regimes_covered = list(paper_summary.get("regimes_covered", [])) if paper_summary else []
    regime_span_check: dict[str, object] = {"covered": regimes_covered, "pass": True}
    if len(regimes_covered) < 2:
        regime_span_check["warning"] = (
            "Paper-trade sessions do not span ≥2 regimes "
            "(recommended: trending + mean_reverting). Increase test diversity."
        )
    checks["regime_span"] = regime_span_check

    passed = all(bool(v.get("pass", True)) for v in checks.values())
    return passed, {
        "mode": mode,
        "checks": checks,
        "paper_trade_summary_source": summary_source,
        "paper_trade_summary_error": summary_error,
        "paper_trade_summary": paper_summary,
    }


def _evaluate_gate_f(config: PromotionConfig, root: Path) -> tuple[bool, dict[str, Any]]:
    if not bool(config.enable_rust_readiness_gate):
        return True, {"skipped": True, "reason": "enable_rust_readiness_gate=false", "checks": {}}

    rust_module = str(config.rust_module_name or "").strip() or _load_rust_module_name(root, config.alpha_id)
    checks: dict[str, dict[str, Any]] = {
        "rust_module_declared": {
            "value": rust_module or None,
            "required": True,
            "pass": bool(rust_module),
        }
    }
    if not rust_module:
        checks["rust_parity_tests"] = {
            "pass": False,
            "detail": "Skipped because rust_module is not declared in manifest",
        }
        if config.enforce_rust_benchmark_gate:
            checks["rust_perf_regression_gate"] = {
                "pass": False,
                "detail": "Skipped because rust_module is not declared in manifest",
            }
        return False, {"checks": checks, "rust_module": None}

    parity_path = Path(config.rust_parity_test_path)
    if not parity_path.is_absolute():
        parity_path = root / parity_path
    parity_cmd = ["uv", "run", "pytest", "-q", "--no-cov", str(parity_path)]
    try:
        parity_proc = subprocess.run(
            parity_cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(10, int(config.rust_parity_timeout_s)),
            check=False,
        )
        checks["rust_parity_tests"] = {
            "command": " ".join(parity_cmd),
            "returncode": int(parity_proc.returncode),
            "stdout_tail": parity_proc.stdout[-2000:],
            "stderr_tail": parity_proc.stderr[-2000:],
            "pass": parity_proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        checks["rust_parity_tests"] = {
            "command": " ".join(parity_cmd),
            "returncode": 124,
            "stdout_tail": (exc.stdout or "")[-2000:],
            "stderr_tail": (exc.stderr or "")[-2000:],
            "pass": False,
            "detail": f"timeout after {int(config.rust_parity_timeout_s)}s",
        }

    if bool(config.enforce_rust_benchmark_gate):
        bench_cmd = shlex.split(str(config.rust_benchmark_cmd))
        try:
            bench_proc = subprocess.run(
                bench_cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(10, int(config.rust_parity_timeout_s)),
                check=False,
            )
            checks["rust_perf_regression_gate"] = {
                "command": " ".join(bench_cmd),
                "returncode": int(bench_proc.returncode),
                "stdout_tail": bench_proc.stdout[-2000:],
                "stderr_tail": bench_proc.stderr[-2000:],
                "pass": bench_proc.returncode == 0,
            }
        except subprocess.TimeoutExpired as exc:
            checks["rust_perf_regression_gate"] = {
                "command": " ".join(bench_cmd),
                "returncode": 124,
                "stdout_tail": (exc.stdout or "")[-2000:],
                "stderr_tail": (exc.stderr or "")[-2000:],
                "pass": False,
                "detail": f"timeout after {int(config.rust_parity_timeout_s)}s",
            }

    passed = all(bool(v.get("pass", False)) for v in checks.values())
    return passed, {"checks": checks, "rust_module": rust_module}


def _resolve_paper_trade_summary(
    config: PromotionConfig,
    root: Path,
) -> tuple[dict[str, Any] | None, str, str | None]:
    if config.paper_trade_summary_path:
        summary_path = Path(config.paper_trade_summary_path)
        if not summary_path.is_absolute():
            summary_path = root / summary_path
        if not summary_path.exists():
            return None, "explicit", f"paper_trade_summary_path_not_found:{summary_path}"
        try:
            payload = json.loads(summary_path.read_text())
        except (OSError, ValueError) as exc:
            return None, "explicit", f"paper_trade_summary_read_error:{exc}"
        if not isinstance(payload, dict):
            return None, "explicit", "paper_trade_summary_invalid_format"
        return payload, "explicit", None

    try:
        from hft_platform.alpha.experiments import ExperimentTracker

        base = root / config.experiments_dir
        tracker = ExperimentTracker(base_dir=base)
        summary = tracker.summarize_paper_trade(config.alpha_id)
        if not isinstance(summary, dict):
            return None, "tracker", "paper_trade_summary_invalid_format"
        if int(summary.get("session_count", 0)) <= 0:
            return None, "tracker", "paper_trade_sessions_missing"
        return summary, "tracker", None
    except Exception as exc:
        return None, "tracker", f"paper_trade_summary_tracker_error:{exc}"


def _load_rust_module_name(root: Path, alpha_id: str) -> str:
    try:
        from research.registry.alpha_registry import AlphaRegistry

        registry = AlphaRegistry()
        loaded = registry.discover(str(root / "research" / "alphas"))
        alpha = loaded.get(alpha_id)
        if alpha is None:
            return ""
        rust_module = getattr(alpha.manifest, "rust_module", None)
        return str(rust_module or "").strip()
    except Exception:
        return ""


def build_promotion_checklist(
    config: PromotionConfig,
    gate_d_checks: dict[str, Any],
    gate_e_checks: dict[str, Any],
    gate_f_checks: dict[str, Any] | None = None,
) -> PromotionChecklist:
    gate_e_map = gate_e_checks.get("checks", gate_e_checks)
    gate_f_map = (gate_f_checks or {}).get("checks", gate_f_checks or {})

    sharpe = gate_d_checks.get("sharpe_oos", {})
    drawdown = gate_d_checks.get("max_drawdown", {})
    turnover = gate_d_checks.get("turnover", {})
    shadow = gate_e_map.get("shadow_sessions", {})
    drift = gate_e_map.get("drift_alerts", {})
    reject = gate_e_map.get("execution_reject_rate", {})
    paper_log = gate_e_map.get("paper_trade_log_available", {})
    paper_span = gate_e_map.get("paper_trade_calendar_days", {})
    paper_days = gate_e_map.get("paper_trade_trading_days", {})
    paper_session_duration = gate_e_map.get("paper_trade_session_duration", {})

    latency = gate_d_checks.get("latency_profile", {})

    items = [
        PromotionChecklistItem(
            label="Gate D: OOS Sharpe >= threshold",
            passed=bool(sharpe.get("pass", False)),
            detail=f"sharpe_oos={sharpe.get('value')} (min={config.min_sharpe_oos})",
        ),
        PromotionChecklistItem(
            label="Gate D: Max drawdown within limit",
            passed=bool(drawdown.get("pass", False)),
            detail=f"max_drawdown={drawdown.get('value')} (min={-abs(config.max_abs_drawdown)})",
        ),
        PromotionChecklistItem(
            label="Gate D: Turnover within limit",
            passed=bool(turnover.get("pass", False)),
            detail=f"turnover={turnover.get('value')} (max={config.max_turnover})",
        ),
        PromotionChecklistItem(
            label="Gate D: Latency profile recorded (constitution requirement)",
            passed=bool(latency.get("pass", False)),
            detail=str(latency.get("detail", "latency_profile not checked")),
        ),
        PromotionChecklistItem(
            label="Gate E: Sufficient shadow sessions",
            passed=bool(shadow.get("pass", False)),
            detail=f"sessions={shadow.get('value')} (min={config.min_shadow_sessions})",
        ),
        PromotionChecklistItem(
            label="Gate E: No drift alerts",
            passed=bool(drift.get("pass", False)),
            detail=f"drift_alerts={drift.get('value')} (max=0)",
        ),
        PromotionChecklistItem(
            label="Gate E: Execution reject rate within limit",
            passed=bool(reject.get("pass", False)),
            detail=f"reject_rate={reject.get('value')} (max={config.max_execution_reject_rate})",
        ),
    ]
    if bool(config.require_paper_trade_governance):
        items.extend(
            [
                PromotionChecklistItem(
                    label="Gate E: Paper-trade log is available",
                    passed=bool(paper_log.get("pass", False)),
                    detail=f"source={paper_log.get('source')} error={paper_log.get('error')}",
                ),
                PromotionChecklistItem(
                    label="Gate E: Paper-trade calendar span >= 7 days",
                    passed=bool(paper_span.get("pass", False)),
                    detail=(
                        f"calendar_span_days={paper_span.get('value')} (min={config.min_paper_trade_calendar_days})"
                    ),
                ),
                PromotionChecklistItem(
                    label="Gate E: Paper-trade distinct trading days >= 5",
                    passed=bool(paper_days.get("pass", False)),
                    detail=(f"trading_days={paper_days.get('value')} (min={config.min_paper_trade_trading_days})"),
                ),
                PromotionChecklistItem(
                    label="Gate E: Paper-trade session duration governance",
                    passed=bool(paper_session_duration.get("pass", False)),
                    detail=(
                        f"min_session_seconds={paper_session_duration.get('value')} "
                        f"(min={int(config.min_paper_trade_session_minutes) * 60}, "
                        f"invalid={paper_session_duration.get('invalid_session_duration_count')})"
                    ),
                ),
            ]
        )
    if bool(config.enable_rust_readiness_gate):
        rust_module = gate_f_map.get("rust_module_declared", {})
        rust_parity = gate_f_map.get("rust_parity_tests", {})
        items.extend(
            [
                PromotionChecklistItem(
                    label="Gate F: Rust module declared in manifest",
                    passed=bool(rust_module.get("pass", False)),
                    detail=f"rust_module={rust_module.get('value')}",
                ),
                PromotionChecklistItem(
                    label="Gate F: Rust parity tests pass",
                    passed=bool(rust_parity.get("pass", False)),
                    detail=f"returncode={rust_parity.get('returncode')}",
                ),
            ]
        )
        if bool(config.enforce_rust_benchmark_gate):
            rust_bench = gate_f_map.get("rust_perf_regression_gate", {})
            items.append(
                PromotionChecklistItem(
                    label="Gate F: Rust performance regression gate pass",
                    passed=bool(rust_bench.get("pass", False)),
                    detail=f"returncode={rust_bench.get('returncode')}",
                )
            )
    return PromotionChecklist(items=items)


def _suggest_canary_weight(scorecard: dict[str, Any], override: float | None = None) -> float:
    if override is not None:
        return max(0.0, float(override))

    sharpe = _to_float(scorecard.get("sharpe_oos")) or 0.0
    if sharpe >= 2.0:
        return 0.10
    if sharpe >= 1.5:
        return 0.07
    if sharpe >= 1.0:
        return 0.05
    return 0.02


def _write_promotion_config(
    root: Path,
    config: PromotionConfig,
    canary_weight: float,
    expiry_review_date: str,
    scorecard: dict[str, Any],
    approved: bool,
    forced: bool,
) -> Path:
    day = datetime.now(UTC).strftime("%Y%m%d")
    out = root / "config" / "strategy_promotions" / day / f"{config.alpha_id}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "alpha_id": config.alpha_id,
        "enabled": bool(approved),
        "weight": float(canary_weight),
        "owner": config.owner,
        "expiry_review_date": expiry_review_date,
        "forced": forced,
        "source_commit": _git_commit(root),
        "config_version": config.config_version,
        "parent_config_version": config.parent_config_version,
        "scorecard_snapshot": {
            "sharpe_oos": _to_float(scorecard.get("sharpe_oos")),
            "max_drawdown": _to_float(scorecard.get("max_drawdown")),
            "turnover": _to_float(scorecard.get("turnover")),
            "correlation_pool_max": _to_float(scorecard.get("correlation_pool_max")),
        },
        "thresholds": {
            "min_sharpe_oos": config.min_sharpe_oos,
            "max_abs_drawdown": config.max_abs_drawdown,
            "max_turnover": config.max_turnover,
            "max_correlation": config.max_correlation,
            "min_shadow_sessions": config.min_shadow_sessions,
            "min_paper_trade_calendar_days": config.min_paper_trade_calendar_days,
            "min_paper_trade_trading_days": config.min_paper_trade_trading_days,
            "min_paper_trade_session_minutes": int(config.min_paper_trade_session_minutes),
        },
        "guardrails": {
            "max_live_slippage_bps": config.max_live_slippage_bps,
            "max_live_drawdown_contribution": config.max_live_drawdown_contribution,
            "max_execution_error_rate": config.max_execution_error_rate,
        },
        "readiness": {
            "require_paper_trade_governance": bool(config.require_paper_trade_governance),
            "enable_rust_readiness_gate": bool(config.enable_rust_readiness_gate),
            "rust_module_name": config.rust_module_name,
        },
        "rollback": {
            "trigger": {
                "live_slippage_bps_gt": config.max_live_slippage_bps,
                "live_drawdown_contribution_gt": config.max_live_drawdown_contribution,
                "execution_error_rate_gt": config.max_execution_error_rate,
            },
            "action": {
                "set_weight_to_zero": True,
                "open_incident": True,
            },
        },
    }
    out.write_text(yaml.safe_dump(payload, sort_keys=False))
    return out


def _git_commit(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
