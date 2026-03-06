"""Versioned latency profile loader for alpha research validation.

Profiles are stored in config/research/latency_profiles.yaml, allowing
P95 broker RTT measurements to be versioned and referenced by profile_id
without hardcoding values in ValidationConfig.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_PROFILES_PATH = Path(__file__).resolve().parents[2] / "config" / "research" / "latency_profiles.yaml"

_REQUIRED_KEYS = frozenset(
    {
        "submit_ack_latency_ms",
        "modify_ack_latency_ms",
        "cancel_ack_latency_ms",
        "local_decision_pipeline_latency_us",
    }
)


def load_latency_profile(profile_id: str, profiles_path: Path | None = None) -> dict[str, Any]:
    """Load latency values for a named profile from the YAML registry.

    Args:
        profile_id: Identifier matching a key under ``profiles:`` in the YAML.
        profiles_path: Override path to the YAML file (defaults to
            ``config/research/latency_profiles.yaml`` relative to the project root).

    Returns:
        Dict with at minimum the keys:
          - ``submit_ack_latency_ms`` (float)
          - ``modify_ack_latency_ms`` (float)
          - ``cancel_ack_latency_ms`` (float)
          - ``local_decision_pipeline_latency_us`` (int)
        Plus any additional metadata fields present in the YAML entry.

    Raises:
        KeyError: If the profile_id is not found in the registry.
        ImportError: If PyYAML is not installed.
        FileNotFoundError: If the profiles YAML file cannot be found.
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required to load latency profiles. "
            "Install it with: pip install pyyaml"
        ) from exc

    path = (profiles_path or _PROFILES_PATH).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Latency profiles YAML not found: {path}")

    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    profiles: dict[str, Any] = raw.get("profiles", {}) if isinstance(raw, dict) else {}

    if profile_id not in profiles:
        available = sorted(profiles.keys())
        raise KeyError(
            f"Latency profile {profile_id!r} not found in {path}. "
            f"Available profiles: {available}"
        )

    profile: dict[str, Any] = dict(profiles[profile_id])
    missing = _REQUIRED_KEYS - profile.keys()
    if missing:
        raise KeyError(
            f"Latency profile {profile_id!r} is missing required fields: {sorted(missing)}"
        )

    # Coerce numeric types for safety
    profile["submit_ack_latency_ms"] = float(profile["submit_ack_latency_ms"])
    profile["modify_ack_latency_ms"] = float(profile["modify_ack_latency_ms"])
    profile["cancel_ack_latency_ms"] = float(profile["cancel_ack_latency_ms"])
    profile["local_decision_pipeline_latency_us"] = int(profile["local_decision_pipeline_latency_us"])

    return profile
