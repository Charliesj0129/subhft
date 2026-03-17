"""Promotion helper utilities — scorecard resolution, checklist, config writing."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from hft_platform.alpha._promotion_types import (
    PromotionChecklist,
    PromotionChecklistItem,
    PromotionConfig,
)


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
