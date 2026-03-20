"""Latency profile registry — load and validate broker latency profiles."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import yaml


def load_latency_profiles(
    project_root: str | Path | None = None,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load latency profiles from config/research/latency_profiles.yaml."""
    if path is not None:
        yaml_path = Path(path)
    elif project_root is not None:
        yaml_path = Path(project_root) / "config" / "research" / "latency_profiles.yaml"
    else:
        return {}
    with contextlib.suppress(Exception):
        raw = yaml.safe_load(yaml_path.read_text())
        if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
            profiles: dict[str, Any] = raw["profiles"]
            return profiles if profiles else {}
    return {}


def validate_latency_profile_id(
    profile_id: str,
    profiles: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Validate profile_id is in the registry."""
    if not profiles:
        return True, "skipped — no profiles registry available"
    if profile_id in profiles:
        return True, f"OK — profile '{profile_id}' found in registry"
    known = ", ".join(sorted(profiles.keys()))
    return False, f"profile_id '{profile_id}' not found in registry; known: {known}"
