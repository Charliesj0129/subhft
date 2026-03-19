"""Latency profile registry for alpha governance.

Validates that latency_profile_id references a known profile from
``config/research/latency_profiles.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger("alpha_latency_registry")

_DEFAULT_PROFILES_REL = "config/research/latency_profiles.yaml"


def load_latency_profiles(
    project_root: str | None = None,
    *,
    path: str | None = None,
) -> dict[str, Any]:
    """Load latency profiles from YAML config.

    Args:
        project_root: Project root directory.
        path: Explicit path override for the profiles file.

    Returns:
        Dict of profile_id -> profile data.  Empty dict if file missing.
    """
    if path is not None:
        profiles_path = Path(path)
    elif project_root is not None:
        profiles_path = Path(project_root) / _DEFAULT_PROFILES_REL
    else:
        profiles_path = Path(".") / _DEFAULT_PROFILES_REL

    if not profiles_path.exists():
        return {}

    try:
        import yaml

        with open(profiles_path) as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        logger.warning("Failed to load latency profiles", path=str(profiles_path))
        return {}


def validate_latency_profile_id(
    profile_id: str,
    profiles: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Check if a latency_profile_id is known in the registry.

    Args:
        profile_id: The profile ID to validate.
        profiles: Pre-loaded profiles dict.  If None, validation is skipped.

    Returns:
        Tuple of (is_valid, detail_message).
    """
    if profiles is None or len(profiles) == 0:
        return True, "skipped — no profiles loaded"

    if profile_id in profiles:
        return True, f"profile_id '{profile_id}' found in registry"

    return False, f"profile_id '{profile_id}' not found in registry"
