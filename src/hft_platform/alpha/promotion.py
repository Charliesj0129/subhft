from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PromotionConfig:
    alpha_id: str
    owner: str
    project_root: str = "."
    scorecard_path: str | None = None
    shadow_sessions: int = 0
    min_shadow_sessions: int = 5
    drift_alerts: int = 0
    execution_reject_rate: float = 0.0
    max_execution_reject_rate: float = 0.01
    min_sharpe_oos: float = 1.0
    max_abs_drawdown: float = 0.2
    max_turnover: float = 2.0
    max_correlation: float = 0.7
    canary_weight: float | None = None
    expiry_days: int = 30
    max_live_slippage_bps: float = 3.0
    max_live_drawdown_contribution: float = 0.02
    max_execution_error_rate: float = 0.01
    force: bool = False


@dataclass(frozen=True)
class PromotionResult:
    alpha_id: str
    approved: bool
    forced: bool
    gate_d_passed: bool
    gate_e_passed: bool
    canary_weight: float
    integration_report_path: str
    promotion_decision_path: str
    promotion_config_path: str | None
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def promote_alpha(config: PromotionConfig) -> PromotionResult:
    root = Path(config.project_root).resolve()
    alpha_dir = root / "research" / "alphas" / config.alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)

    scorecard_path = Path(config.scorecard_path) if config.scorecard_path else (alpha_dir / "scorecard.json")
    if not scorecard_path.exists():
        raise FileNotFoundError(f"scorecard not found: {scorecard_path}")

    scorecard = json.loads(scorecard_path.read_text())
    gate_d_passed, gate_d_checks = _evaluate_gate_d(scorecard, config)
    gate_e_passed, gate_e_checks = _evaluate_gate_e(config)

    approved = gate_d_passed and gate_e_passed
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

    integration_report = {
        "alpha_id": config.alpha_id,
        "gate": "Gate D",
        "passed": gate_d_passed,
        "checks": gate_d_checks,
        "scorecard_path": str(scorecard_path),
        "timestamp": now.isoformat(),
    }
    integration_report_path = alpha_dir / "integration_report.json"
    _write_json(integration_report_path, integration_report)

    decision_payload = {
        "alpha_id": config.alpha_id,
        "gate_d_passed": gate_d_passed,
        "gate_e_passed": gate_e_passed,
        "gate_e_checks": gate_e_checks,
        "decision": "approve" if approved else "reject",
        "forced": forced,
        "reasons": reasons,
        "canary_weight": weight,
        "timestamp": now.isoformat(),
    }
    decision_path = alpha_dir / "promotion_decision.json"
    _write_json(decision_path, decision_payload)

    promotion_config_path: Path | None = None
    if approved:
        promotion_config_path = _write_promotion_config(
            root=root,
            config=config,
            canary_weight=weight,
            expiry_review_date=expiry,
            scorecard=scorecard,
            approved=approved,
            forced=forced,
        )

    result = PromotionResult(
        alpha_id=config.alpha_id,
        approved=approved,
        forced=forced,
        gate_d_passed=gate_d_passed,
        gate_e_passed=gate_e_passed,
        canary_weight=weight,
        integration_report_path=str(integration_report_path),
        promotion_decision_path=str(decision_path),
        promotion_config_path=str(promotion_config_path) if promotion_config_path else None,
        reasons=reasons,
    )

    # Best-effort audit logging (guarded by HFT_ALPHA_AUDIT_ENABLED)
    try:
        from hft_platform.alpha.audit import log_gate_result, log_promotion_result
        from hft_platform.alpha.validation import GateReport

        gate_d_report = GateReport(gate="Gate D", passed=gate_d_passed, details=gate_d_checks)
        gate_e_report = GateReport(gate="Gate E", passed=gate_e_passed, details=gate_e_checks)
        cfg_hash = scorecard.get("config_hash", "")
        for gate_report in (gate_d_report, gate_e_report):
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

    checks = {
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
            "pass": (corr is None or corr <= config.max_correlation),
        },
    }
    passed = all(bool(v["pass"]) for v in checks.values())
    return passed, checks


def _evaluate_gate_e(config: PromotionConfig) -> tuple[bool, dict[str, Any]]:
    checks = {
        "shadow_sessions": {
            "value": config.shadow_sessions,
            "min": config.min_shadow_sessions,
            "pass": config.shadow_sessions >= config.min_shadow_sessions,
        },
        "drift_alerts": {"value": config.drift_alerts, "max": 0, "pass": config.drift_alerts == 0},
        "execution_reject_rate": {
            "value": config.execution_reject_rate,
            "max": config.max_execution_reject_rate,
            "pass": config.execution_reject_rate <= config.max_execution_reject_rate,
        },
    }
    passed = all(bool(v["pass"]) for v in checks.values())
    return passed, checks


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
        },
        "guardrails": {
            "max_live_slippage_bps": config.max_live_slippage_bps,
            "max_live_drawdown_contribution": config.max_live_drawdown_contribution,
            "max_execution_error_rate": config.max_execution_error_rate,
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
