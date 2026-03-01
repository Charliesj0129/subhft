from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StrategyFeatureCompatibilityIssue:
    strategy_id: str
    level: str  # error|warning
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "strategy_id": self.strategy_id,
            "level": self.level,
            "code": self.code,
            "message": self.message,
        }


def _safe_get_feature_set_id(feature_engine: Any) -> str | None:
    if feature_engine is None:
        return None
    fn = getattr(feature_engine, "feature_set_id", None)
    if not callable(fn):
        return None
    try:
        return str(fn())
    except Exception:
        return None


def _safe_get_feature_ids(feature_engine: Any) -> set[str]:
    if feature_engine is None:
        return set()
    fn = getattr(feature_engine, "feature_ids", None)
    if not callable(fn):
        return set()
    try:
        return {str(x) for x in (fn() or ())}
    except Exception:
        return set()


def _safe_get_schema_version(feature_engine: Any) -> int | None:
    if feature_engine is None:
        return None
    fn = getattr(feature_engine, "schema_version", None)
    if not callable(fn):
        return None
    try:
        return int(fn())
    except Exception:
        return None


def _safe_get_profile_id(feature_engine: Any) -> str | None:
    if feature_engine is None:
        return None
    for attr in ("active_profile_id", "profile_id"):
        fn = getattr(feature_engine, attr, None)
        if callable(fn):
            try:
                out = fn()
                return str(out) if out else None
            except Exception:
                continue
    return None


def check_strategy_feature_compat(strategy: Any, feature_engine: Any) -> list[StrategyFeatureCompatibilityIssue]:
    sid = str(getattr(strategy, "strategy_id", "unknown"))
    issues: list[StrategyFeatureCompatibilityIssue] = []

    req_set = getattr(strategy, "required_feature_set_id", None)
    req_schema = getattr(strategy, "required_feature_schema_version", None)
    req_features = list(getattr(strategy, "required_feature_ids", None) or [])
    opt_features = list(getattr(strategy, "optional_feature_ids", None) or [])
    req_profile = getattr(strategy, "required_feature_profile_id", None)

    if any((req_set, req_schema, req_features, opt_features, req_profile)) and feature_engine is None:
        issues.append(
            StrategyFeatureCompatibilityIssue(
                sid,
                "error",
                "feature_engine_missing",
                "Strategy declares feature dependencies but FeatureEngine is disabled/missing",
            )
        )
        return issues

    fe_set = _safe_get_feature_set_id(feature_engine)
    fe_schema = _safe_get_schema_version(feature_engine)
    fe_profile = _safe_get_profile_id(feature_engine)
    fe_ids = _safe_get_feature_ids(feature_engine)

    if req_set and fe_set and str(req_set) != fe_set:
        issues.append(
            StrategyFeatureCompatibilityIssue(
                sid,
                "error",
                "feature_set_mismatch",
                f"required_feature_set_id={req_set!r}, runtime={fe_set!r}",
            )
        )
    if req_set and fe_set is None:
        issues.append(
            StrategyFeatureCompatibilityIssue(
                sid, "error", "feature_set_unavailable", "Runtime feature set id unavailable"
            )
        )

    if req_schema is not None and fe_schema is not None and int(req_schema) > int(fe_schema):
        issues.append(
            StrategyFeatureCompatibilityIssue(
                sid,
                "error",
                "feature_schema_too_old",
                f"required schema {int(req_schema)} > runtime schema {int(fe_schema)}",
            )
        )

    if req_profile and fe_profile and str(req_profile) != str(fe_profile):
        issues.append(
            StrategyFeatureCompatibilityIssue(
                sid,
                "error",
                "feature_profile_mismatch",
                f"required_feature_profile_id={req_profile!r}, runtime={fe_profile!r}",
            )
        )
    elif req_profile and fe_profile is None:
        issues.append(
            StrategyFeatureCompatibilityIssue(
                sid,
                "warning",
                "feature_profile_unavailable",
                "Strategy declares required_feature_profile_id but runtime does not expose active profile id",
            )
        )

    for fid in req_features:
        if str(fid) not in fe_ids:
            issues.append(
                StrategyFeatureCompatibilityIssue(
                    sid,
                    "error",
                    "required_feature_missing",
                    f"Missing required feature '{fid}' in runtime feature set",
                )
            )
    for fid in opt_features:
        if str(fid) not in fe_ids:
            issues.append(
                StrategyFeatureCompatibilityIssue(
                    sid,
                    "warning",
                    "optional_feature_missing",
                    f"Optional feature '{fid}' not available in runtime feature set",
                )
            )
    return issues


def check_strategies_feature_compat(
    strategies: list[Any], feature_engine: Any
) -> list[StrategyFeatureCompatibilityIssue]:
    out: list[StrategyFeatureCompatibilityIssue] = []
    for strat in strategies:
        out.extend(check_strategy_feature_compat(strat, feature_engine))
    return out
