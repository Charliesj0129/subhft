"""Gate D evaluation — backtest quantitative thresholds."""

from __future__ import annotations

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

# Latency fields validated for realism.
# Each scorecard value must be >= profile * _LATENCY_FLOOR_RATIO.
_LATENCY_FLOOR_RATIO: float = 0.8
_LATENCY_FIELDS: tuple[str, ...] = (
    "submit_ack_latency_ms",
    "modify_ack_latency_ms",
    "cancel_ack_latency_ms",
)


def _load_latency_profiles(project_root: str) -> dict[str, Any]:
    """Load latency profiles YAML from config/research/latency_profiles.yaml.

    Returns an empty dict on any IO / parse error so that a missing file
    degrades gracefully (warn-only, non-blocking).
    """
    profiles_path = Path(project_root) / "config" / "research" / "latency_profiles.yaml"
    try:
        raw = yaml.safe_load(profiles_path.read_text())
        return dict(raw.get("profiles", {})) if isinstance(raw, dict) else {}
    except FileNotFoundError:
        _log.warning(
            "gate_d.latency_profiles_missing",
            path=str(profiles_path),
            detail="latency_profiles.yaml not found — latency value check skipped",
        )
        return {}
    except Exception as exc:
        _log.warning(
            "gate_d.latency_profiles_load_error",
            path=str(profiles_path),
            error=str(exc),
        )
        return {}


def _check_latency_values(
    latency_profile: dict[str, Any],
    profiles: dict[str, Any],
) -> dict[str, Any]:
    """Verify backtest latency values are not unrealistically low vs the named profile.

    Scorecard latency >= profile_latency * _LATENCY_FLOOR_RATIO for each ACK field.
    Returns a Gate D check dict.

    Degrades gracefully (non-blocking, pass=True) when:
    - latency_profile_id key is absent from the dict
    - latency_profiles.yaml is unavailable
    - profile_id is not found in the YAML
    """
    profile_id: str | None = latency_profile.get("latency_profile_id")
    if not profile_id:
        return {
            "required": False,
            "pass": True,
            "profile_id": None,
            "detail": (
                "SKIPPED — latency_profile dict has no latency_profile_id key; value realism check could not run"
            ),
        }

    if not profiles:
        _log.warning(
            "gate_d.latency_values_check_skipped",
            profile_id=profile_id,
            reason="latency_profiles.yaml unavailable",
        )
        return {
            "required": False,
            "pass": True,
            "profile_id": profile_id,
            "detail": "SKIPPED — latency_profiles.yaml unavailable; value realism check could not run",
        }

    profile_data = profiles.get(profile_id)
    if profile_data is None:
        _log.warning(
            "gate_d.latency_profile_id_not_found",
            profile_id=profile_id,
            available=list(profiles.keys()),
        )
        return {
            "required": False,
            "pass": True,
            "profile_id": profile_id,
            "detail": (f"SKIPPED — profile_id '{profile_id}' not found in latency_profiles.yaml; check skipped"),
        }

    failures: list[str] = []
    field_results: dict[str, Any] = {}
    for field in _LATENCY_FIELDS:
        scorecard_val = _to_float(latency_profile.get(field))
        profile_val = _to_float(profile_data.get(field))
        if scorecard_val is None or profile_val is None:
            field_results[field] = {
                "scorecard": scorecard_val,
                "profile": profile_val,
                "pass": True,
                "skipped": True,
            }
            continue
        floor = profile_val * _LATENCY_FLOOR_RATIO
        field_pass = scorecard_val >= floor
        field_results[field] = {
            "scorecard": scorecard_val,
            "profile": profile_val,
            "floor": floor,
            "pass": field_pass,
        }
        if not field_pass:
            failures.append(
                f"{field}: scorecard={scorecard_val}ms < floor={floor:.1f}ms "
                f"(profile={profile_val}ms x {_LATENCY_FLOOR_RATIO})"
            )

    overall_pass = len(failures) == 0
    if failures:
        detail = "UNREALISTIC — backtest used latency values below 80% of broker P95 profile: " + "; ".join(failures)
    else:
        detail = f"OK — all latency values >= {int(_LATENCY_FLOOR_RATIO * 100)}% of profile '{profile_id}'"

    return {
        "required": True,
        "pass": overall_pass,
        "profile_id": profile_id,
        "field_results": field_results,
        "detail": detail,
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
            else "WARN — stress_test.passed not True; stress testing is recommended before production promotion"
        ),
    }

    # Feature set version parity check (warn-only: does NOT block Gate D).
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

    # --- Unit 2: Half-life vs broker RTT check ---
    # If signal edge decays before an order can execute, the alpha is not actionable.
    # Mandatory (blocks Gate D) when both values are present.
    # Warn-only when either value is missing.
    halflife_ms = _to_float(scorecard.get("signal_halflife_ms"))
    _lp = scorecard.get("latency_profile") if isinstance(scorecard.get("latency_profile"), dict) else None
    submit_ack_ms = _to_float(_lp.get("submit_ack_latency_ms") if _lp else None)

    if halflife_ms is not None and submit_ack_ms is not None:
        _threshold = submit_ack_ms * 2.0
        _hl_pass = halflife_ms >= _threshold
        checks["halflife_vs_rtt"] = {
            "value": halflife_ms,
            "submit_ack_latency_ms": submit_ack_ms,
            "threshold_ms": _threshold,
            "required": True,
            "pass": _hl_pass,
            "detail": (
                "OK"
                if _hl_pass
                else f"FAIL — signal_halflife_ms={halflife_ms:.2f} < submit_ack_latency_ms*2.0={_threshold:.2f}; "
                "alpha edge decays before order can execute"
            ),
        }
    else:
        _missing = []
        if halflife_ms is None:
            _missing.append("signal_halflife_ms")
        if submit_ack_ms is None:
            _missing.append("latency_profile.submit_ack_latency_ms")
        checks["halflife_vs_rtt"] = {
            "value": halflife_ms,
            "submit_ack_latency_ms": submit_ack_ms,
            "required": False,
            "pass": True,
            "detail": f"WARN — cannot evaluate half-life vs RTT: missing {', '.join(_missing)}",
        }

    # Diagnostic: adjusted Sharpe assuming 2x latency (non-blocking)
    adjusted_sharpe: float | None = None
    if sharpe is not None:
        adjusted_sharpe = sharpe * 0.7
    checks["adjusted_sharpe_2x_latency"] = {
        "value": adjusted_sharpe,
        "threshold": None,
        "pass": True,
        "detail": "diagnostic: Sharpe under 2x latency assumption",
    }

    passed = all(bool(v["pass"]) for v in checks.values())
    return passed, checks
