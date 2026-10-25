from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.alpha.paths import repo_path

logger = get_logger("alpha.latency_profiles")

# Relative to the repo root, not the cwd (see ``alpha.paths``).
_DEFAULT_PROFILES_PATH = Path("config/research/latency_profiles.yaml")

# Ids accepted besides the exact profile keys. There is no prefix matching: a
# prefix silently picked whichever profile happened to share it.
_ALIASES: dict[str, str] = {
    "sim_p95_v2026-02-26": "shioaji_sim_p95_v2026-02-26",
    "sim_p95_v2026-03-01": "shioaji_sim_p95_v2026-03-01",
    "sim_p95_v2026-03-04": "shioaji_sim_p95_v2026-03-04",
    "fubon_p95_v2026-03-12": "fubon_estimated_p95_v2026-03-12",
    # r52's manifest names the 2026-04-24 measurement without its broker prefix.
    "v2026-04-24_measured": "r47_maker_shioaji_p95_v2026-04-24_measured",
}

_REQUIRED_FIELDS = (
    "submit_ack_latency_ms",
    "modify_ack_latency_ms",
    "cancel_ack_latency_ms",
    "local_decision_pipeline_latency_us",
)


def load_profiles(yaml_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load latency profiles from YAML. Returns dict[profile_id, profile_dict].

    Raises ``FileNotFoundError`` when the file is missing and ``ValueError``
    when it holds no ``profiles`` mapping: no profiles is not a usable answer.
    """
    path = Path(yaml_path) if yaml_path else repo_path(_DEFAULT_PROFILES_PATH)
    if not path.exists():
        raise FileNotFoundError(f"latency profiles file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    profiles = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError(f"latency profiles file has no 'profiles' mapping: {path}")
    return {str(k): dict(v) for k, v in profiles.items() if isinstance(v, dict)}


def resolve_profile(
    profile_id: str,
    profiles: dict[str, dict[str, Any]] | None = None,
    *,
    yaml_path: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve an exact profile id or a listed alias to the full profile dict.

    Raises ``KeyError`` if the profile cannot be found.
    Raises ``ValueError`` if required fields are missing.
    Raises ``FileNotFoundError`` if the profiles file is missing (see ``load_profiles``).
    """
    if profiles is None:
        profiles = load_profiles(yaml_path)

    # Direct lookup
    if profile_id in profiles:
        profile = dict(profiles[profile_id])
    elif profile_id in _ALIASES and _ALIASES[profile_id] in profiles:
        profile = dict(profiles[_ALIASES[profile_id]])
    else:
        raise KeyError(f"Latency profile {profile_id!r} not found. Available: {sorted(profiles.keys())}")

    missing = [f for f in _REQUIRED_FIELDS if f not in profile]
    if missing:
        raise ValueError(f"Latency profile {profile_id!r} missing required fields: {missing}")
    return profile
