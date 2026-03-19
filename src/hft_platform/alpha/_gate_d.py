"""Gate D evaluation — backtest quantitative thresholds."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

import structlog

from hft_platform.alpha._promotion_helpers import _to_float
from hft_platform.alpha._promotion_types import PromotionConfig

_log = structlog.get_logger(__name__)


def _evaluate_gate_d(scorecard: dict[str, Any], config: PromotionConfig) -> tuple[bool, dict[str, Any]]:
    # Allow env var override for min_sharpe_oos threshold
    env_sharpe = os.getenv("HFT_GATE_D_MIN_SHARPE_OOS")
    if env_sharpe is not None:
        try:
            override_val = float(env_sharpe)
            _log.info("gate_d_sharpe_override", env_value=override_val, original=config.min_sharpe_oos)
            config = replace(config, min_sharpe_oos=override_val)
        except ValueError:
            _log.warning("gate_d_sharpe_override_invalid", env_value=env_sharpe)

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

    # Diagnostic: adjusted Sharpe assuming 2x latency (non-blocking)
    adjusted_sharpe: float | None = None
    if sharpe is not None:
        # Conservative heuristic: 2x latency degrades Sharpe by ~30%
        adjusted_sharpe = sharpe * 0.7
    checks["adjusted_sharpe_2x_latency"] = {
        "value": adjusted_sharpe,
        "threshold": None,
        "pass": True,  # Always passes (diagnostic only)
        "detail": "diagnostic: Sharpe under 2x latency assumption",
    }

    passed = all(bool(v["pass"]) for v in checks.values())
    return passed, checks
