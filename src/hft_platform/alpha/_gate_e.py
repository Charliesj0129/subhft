"""Gate E evaluation — paper-trade governance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hft_platform.alpha._promotion_helpers import _to_float
from hft_platform.alpha._promotion_types import PromotionConfig


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
    # Sessions should cover >=2 distinct regimes for adequate test diversity.
    regimes_covered = list(paper_summary.get("regimes_covered", [])) if paper_summary else []
    regime_span_check: dict[str, object] = {"covered": regimes_covered, "pass": True}
    if len(regimes_covered) < 2:
        regime_span_check["warning"] = (
            "Paper-trade sessions do not span >=2 regimes "
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


def _build_paper_governance_report(
    config: PromotionConfig,
    gate_e_checks: dict[str, Any],
) -> dict[str, Any]:
    gate_e_map_raw = gate_e_checks.get("checks", {})
    gate_e_map = gate_e_map_raw if isinstance(gate_e_map_raw, dict) else {}
    summary_raw = gate_e_checks.get("paper_trade_summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}

    shadow_check = gate_e_map.get("shadow_sessions", {})
    drift_check = gate_e_map.get("drift_alerts", {})
    reject_check = gate_e_map.get("execution_reject_rate", {})

    session_count = int(summary.get("session_count", shadow_check.get("value", 0)))
    calendar_days = int(summary.get("calendar_span_days", 0))
    trading_days = int(summary.get("distinct_trading_days", 0))
    min_session_seconds = int(summary.get("min_session_duration_seconds", 0))
    invalid_duration_count = int(summary.get("invalid_session_duration_count", session_count))
    drift_alerts_total = int(summary.get("drift_alerts_total", drift_check.get("value", 0)))
    reject_rate = _to_float(reject_check.get("value")) or 0.0
    reject_rate_source = str(reject_check.get("source") or "mean")
    regimes_covered_raw = summary.get("regimes_covered", [])
    regimes_covered = [str(x) for x in regimes_covered_raw] if isinstance(regimes_covered_raw, list) else []
    required_session_seconds = max(60, int(config.min_paper_trade_session_minutes) * 60)
    min_required_regimes = 2

    checks: dict[str, dict[str, Any]] = {
        "shadow_sessions": {
            "value": session_count,
            "min": int(config.min_shadow_sessions),
            "pass": session_count >= int(config.min_shadow_sessions),
        },
        "calendar_span_days": {
            "value": calendar_days,
            "min": int(config.min_paper_trade_calendar_days),
            "pass": calendar_days >= int(config.min_paper_trade_calendar_days),
        },
        "trading_days": {
            "value": trading_days,
            "min": int(config.min_paper_trade_trading_days),
            "pass": trading_days >= int(config.min_paper_trade_trading_days),
        },
        "session_duration": {
            "value": min_session_seconds,
            "min_seconds": required_session_seconds,
            "invalid_session_duration_count": invalid_duration_count,
            "pass": (invalid_duration_count == 0 and min_session_seconds >= required_session_seconds),
        },
        "drift_alerts": {
            "value": drift_alerts_total,
            "max": 0,
            "pass": drift_alerts_total <= 0,
        },
        "execution_reject_rate": {
            "value": reject_rate,
            "max": float(config.max_execution_reject_rate),
            "source": reject_rate_source,
            "pass": reject_rate <= float(config.max_execution_reject_rate),
        },
        "regime_span": {
            "value": len(regimes_covered),
            "covered": regimes_covered,
            "min": min_required_regimes,
            "pass": len(regimes_covered) >= min_required_regimes,
        },
    }
    passed = all(bool(item.get("pass", False)) for item in checks.values())
    return {
        "alpha_id": config.alpha_id,
        "passed": passed,
        "checks": checks,
        "summary": (summary if summary else None),
        "paper_trade_summary_source": gate_e_checks.get("paper_trade_summary_source"),
        "paper_trade_summary_error": gate_e_checks.get("paper_trade_summary_error"),
    }
