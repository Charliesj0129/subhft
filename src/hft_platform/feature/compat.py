from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hft_platform.feature.profile import FeatureProfile
from hft_platform.feature.registry import FeatureRegistry


@dataclass(frozen=True, slots=True)
class FeatureCompatibilityIssue:
    level: str  # error|warning
    code: str
    message: str


def check_feature_profile_compat(profile: FeatureProfile, registry: FeatureRegistry) -> list[FeatureCompatibilityIssue]:
    issues: list[FeatureCompatibilityIssue] = []
    try:
        fs = registry.get(profile.feature_set_id)
    except Exception:
        return [
            FeatureCompatibilityIssue(
                "error", "unknown_feature_set", f"Unknown feature_set_id {profile.feature_set_id!r}"
            )
        ]

    if profile.schema_version is not None and int(profile.schema_version) > int(fs.schema_version):
        issues.append(
            FeatureCompatibilityIssue(
                "error",
                "schema_too_new",
                f"profile schema_version={profile.schema_version} > runtime schema_version={fs.schema_version}",
            )
        )
    params = dict(profile.params or {})
    if "ema_window" in params:
        try:
            w = int(params["ema_window"])
            if w <= 0:
                issues.append(FeatureCompatibilityIssue("error", "invalid_ema_window", "ema_window must be > 0"))
            elif w > 512:
                issues.append(
                    FeatureCompatibilityIssue("warning", "large_ema_window", f"ema_window={w} is unusually large")
                )
        except Exception:
            issues.append(FeatureCompatibilityIssue("error", "invalid_ema_window", "ema_window must be integer"))
    return issues


def check_runtime_feature_engine_compat(feature_engine: Any) -> list[FeatureCompatibilityIssue]:
    issues: list[FeatureCompatibilityIssue] = []
    if feature_engine is None:
        return [FeatureCompatibilityIssue("error", "feature_engine_missing", "FeatureEngine is not enabled")]
    for attr in ("feature_set_id", "schema_version", "get_feature", "get_feature_view"):
        fn = getattr(feature_engine, attr, None)
        if not callable(fn):
            issues.append(FeatureCompatibilityIssue("error", "missing_api", f"FeatureEngine missing callable {attr}()"))
    return issues
