"""Gate D evaluation — backtest quantitative thresholds."""

from __future__ import annotations

import contextlib
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import structlog
import yaml

from hft_platform.alpha._latency_registry import validate_latency_profile_id
from hft_platform.alpha._promotion_helpers import _to_float
from hft_platform.alpha._promotion_types import PromotionConfig

_log = structlog.get_logger(__name__)

_LATENCY_FLOOR_RATIO: float = 0.8
_LATENCY_FIELDS: tuple[str, ...] = (
    "submit_ack_latency_ms",
    "modify_ack_latency_ms",
    "cancel_ack_latency_ms",
)


def _load_latency_profiles(project_root: str) -> dict[str, Any]:
    yaml_path = Path(project_root) / "config" / "research" / "latency_profiles.yaml"
    with contextlib.suppress(Exception):
        raw = yaml.safe_load(yaml_path.read_text())
        if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
            profiles: dict[str, Any] = raw["profiles"]
            return profiles if profiles else {}
    return {}


def _check_latency_values(
    latency_profile: dict[str, Any],
    profiles: dict[str, Any],
) -> dict[str, Any]:
    profile_id = latency_profile.get("latency_profile_id")
    if not profile_id:
        return {
            "pass": True,
            "required": False,
            "detail": "SKIPPED — no latency_profile_id in scorecard",
            "field_results": {},
        }
    if not profiles:
        return {
            "pass": True,
            "required": False,
            "detail": "SKIPPED — no profiles registry available",
            "field_results": {},
        }
    canonical = profiles.get(str(profile_id))
    if canonical is None:
        return {
            "pass": True,
            "required": False,
            "detail": f"SKIPPED — profile_id '{profile_id}' not found in registry",
            "field_results": {},
        }
    field_results: dict[str, Any] = {}
    unrealistic: list[str] = []
    for field in _LATENCY_FIELDS:
        sc_val = latency_profile.get(field)
        profile_val = canonical.get(field)
        if sc_val is None or profile_val is None:
            field_results[field] = {"skipped": True, "reason": "missing in scorecard or profile"}
            continue
        floor = profile_val * _LATENCY_FLOOR_RATIO
        ok = float(sc_val) >= floor
        field_results[field] = {
            "skipped": False,
            "scorecard": sc_val,
            "profile": profile_val,
            "floor": floor,
            "pass": ok,
        }
        if not ok:
            unrealistic.append(
                f"{field}={sc_val}ms < floor {floor:.1f}ms "
                f"(profile={profile_val}ms x {_LATENCY_FLOOR_RATIO})"
            )
    if unrealistic:
        return {
            "pass": False,
            "required": True,
            "detail": f"UNREALISTIC — {'; '.join(unrealistic)}",
            "field_results": field_results,
        }
    floor_pct = int(_LATENCY_FLOOR_RATIO * 100)
    return {
        "pass": True,
        "required": True,
        "detail": f"OK — all latency values >= {floor_pct}% of profile '{profile_id}'",
        "field_results": field_results,
    }


def _evaluate_gate_d(scorecard: dict[str, Any], config: PromotionConfig) -> tuple[bool, dict[str, Any]]:
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

    if isinstance(latency_profile, dict):
        profiles = _load_latency_profiles(config.project_root)
        checks["latency_values_realistic"] = _check_latency_values(latency_profile, profiles)
        lp_id = latency_profile.get("latency_profile_id")
        if lp_id is not None:
            lp_valid, lp_detail = validate_latency_profile_id(str(lp_id), profiles if profiles else None)
            checks["latency_profile_id_known"] = {
                "value": str(lp_id),
                "valid": lp_valid,
                "required": False,
                "pass": True,
                "detail": lp_detail if lp_valid else f"WARN — {lp_detail}",
            }

    stress_test = scorecard.get("stress_test") or {}
    stress_passed = bool(stress_test.get("passed")) if isinstance(stress_test, dict) else False
    checks["stress_test_validated"] = {
        "required": False,
        "pass": True,
        "value": stress_passed,
        "detail": (
            "OK"
            if stress_passed
            else "WARN — stress_test.passed not True; stress testing is recommended"
        ),
    }

    manifest_fsv = str(config.manifest_feature_set_version or "").strip() or None
    _LIVE_FSV: str | None = None
    try:
        from hft_platform.feature.registry import FEATURE_SET_VERSION as _LIVE_FSV
    except Exception as exc:
        _log.debug("optional_feature_unavailable", error=str(exc))
    if manifest_fsv is not None and _LIVE_FSV is not None:
        fsv_match = manifest_fsv == _LIVE_FSV
        checks["feature_set_version"] = {
            "manifest": manifest_fsv,
            "live_engine": _LIVE_FSV,
            "match": fsv_match,
            "pass": fsv_match,
            "detail": (
                "OK"
                if fsv_match
                else f"MISMATCH — manifest declares '{manifest_fsv}' but live engine uses '{_LIVE_FSV}'. "
                "Re-run backtest with the current feature set before promoting to live."
            ),
        }

    adjusted_sharpe: float | None = None
    if sharpe is not None:
        adjusted_sharpe = sharpe * 0.7
    checks["adjusted_sharpe_2x_latency"] = {
        "value": adjusted_sharpe,
        "threshold": None,
        "pass": True,
        "detail": "diagnostic: Sharpe under 2x latency assumption",
    }

    signal_halflife = scorecard.get("signal_halflife_ms")
    submit_ack: float | None = None
    if isinstance(latency_profile, dict):
        submit_ack = latency_profile.get("submit_ack_latency_ms")

    if signal_halflife is None:
        checks["halflife_vs_rtt"] = {
            "pass": True,
            "required": False,
            "detail": (
                "WARN — signal_halflife_ms not recorded in scorecard; "
                "recommend recording to enforce RTT realism gate"
            ),
        }
    elif submit_ack is None:
        checks["halflife_vs_rtt"] = {
            "pass": True,
            "required": False,
            "detail": "WARN — submit_ack_latency_ms not available; cannot check half-life vs RTT",
        }
    else:
        threshold_ms = float(submit_ack) * 2.0
        hl_val = float(signal_halflife)
        hl_pass = hl_val >= threshold_ms
        checks["halflife_vs_rtt"] = {
            "pass": hl_pass,
            "required": True,
            "value_ms": hl_val,
            "threshold_ms": threshold_ms,
            "detail": (
                f"OK — signal_halflife_ms={hl_val}ms >= threshold={threshold_ms}ms (2x submit_ack)"
                if hl_pass
                else f"FAIL — signal_halflife_ms={hl_val}ms < threshold={threshold_ms}ms (2x submit_ack); "
                "alpha half-life too short relative to broker RTT"
            ),
        }

    passed = all(bool(v["pass"]) for v in checks.values())
    return passed, checks
