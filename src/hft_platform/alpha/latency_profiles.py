from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

logger = get_logger("alpha.latency_profiles")

_DEFAULT_PROFILES_PATH = Path("config/research/latency_profiles.yaml")

# Common aliases mapping short ids to full profile keys.
_ALIASES: dict[str, str] = {
    "sim_p95_v2026-02-26": "shioaji_sim_p95_v2026-02-26",
    "sim_p95_v2026-03-01": "shioaji_sim_p95_v2026-03-01",
    "sim_p95_v2026-03-04": "shioaji_sim_p95_v2026-03-04",
    "fubon_p95_v2026-03-12": "fubon_estimated_p95_v2026-03-12",
}

_REQUIRED_FIELDS = (
    "submit_ack_latency_ms",
    "modify_ack_latency_ms",
    "cancel_ack_latency_ms",
    "local_decision_pipeline_latency_us",
)


def load_profiles(yaml_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load latency profiles from YAML. Returns dict[profile_id, profile_dict]."""
    path = Path(yaml_path) if yaml_path else _DEFAULT_PROFILES_PATH
    if not path.exists():
        logger.warning("latency_profiles.load_profiles: file not found", path=str(path))
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        return {}
    return {str(k): dict(v) for k, v in profiles.items() if isinstance(v, dict)}


def resolve_profile(
    profile_id: str,
    profiles: dict[str, dict[str, Any]] | None = None,
    *,
    yaml_path: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve a profile id (or alias) to the full profile dict.

    Raises ``KeyError`` if the profile cannot be found.
    Raises ``ValueError`` if required fields are missing.
    """
    if profiles is None:
        profiles = load_profiles(yaml_path)

    # Direct lookup
    if profile_id in profiles:
        profile = dict(profiles[profile_id])
    elif profile_id in _ALIASES and _ALIASES[profile_id] in profiles:
        profile = dict(profiles[_ALIASES[profile_id]])
    else:
        # Try prefix match (e.g. "shioaji_sim_p95" matches "shioaji_sim_p95_v2026-03-04")
        candidates = [k for k in profiles if k.startswith(profile_id)]
        if len(candidates) == 1:
            profile = dict(profiles[candidates[0]])
        elif len(candidates) > 1:
            raise KeyError(f"Ambiguous latency profile id {profile_id!r}: matches {candidates}")
        else:
            raise KeyError(f"Latency profile {profile_id!r} not found. Available: {sorted(profiles.keys())}")

    missing = [f for f in _REQUIRED_FIELDS if f not in profile]
    if missing:
        raise ValueError(f"Latency profile {profile_id!r} missing required fields: {missing}")
    return profile
