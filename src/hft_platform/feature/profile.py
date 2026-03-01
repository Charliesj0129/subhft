from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class FeatureProfile:
    profile_id: str
    feature_set_id: str
    schema_version: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    state: str = "active"  # active|shadow|disabled
    owner: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "feature_set_id": self.feature_set_id,
            "schema_version": self.schema_version,
            "params": dict(self.params or {}),
            "enabled": bool(self.enabled),
            "state": str(self.state),
            "owner": str(self.owner or ""),
            "notes": str(self.notes or ""),
        }


class FeatureProfileRegistry:
    """Config-backed registry for feature parameter profiles.

    Prototype scope:
    - YAML file loading/validation
    - default/active profile resolution
    - no distributed config or runtime mutation bus yet
    """

    __slots__ = ("_profiles", "_default_id", "_path")

    def __init__(self) -> None:
        self._profiles: dict[str, FeatureProfile] = {}
        self._default_id: str | None = None
        self._path: str | None = None

    @property
    def path(self) -> str | None:
        return self._path

    def register(self, profile: FeatureProfile, *, make_default: bool = False) -> None:
        self._profiles[str(profile.profile_id)] = profile
        if make_default or self._default_id is None:
            self._default_id = str(profile.profile_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def get(self, profile_id: str) -> FeatureProfile:
        try:
            return self._profiles[str(profile_id)]
        except KeyError as exc:
            raise KeyError(f"Unknown feature profile: {profile_id}") from exc

    def get_default(self) -> FeatureProfile | None:
        if self._default_id is None:
            return None
        return self._profiles.get(self._default_id)

    def get_active_for_set(self, feature_set_id: str) -> FeatureProfile | None:
        feature_set_id = str(feature_set_id)
        explicit = os.getenv("HFT_FEATURE_PROFILE_ID", "").strip()
        if explicit:
            prof = self._profiles.get(explicit)
            if prof and prof.enabled and prof.feature_set_id == feature_set_id:
                return prof
        default = self.get_default()
        if default and default.enabled and default.feature_set_id == feature_set_id:
            return default
        for prof in self._profiles.values():
            if prof.enabled and prof.state in {"active", "shadow"} and prof.feature_set_id == feature_set_id:
                return prof
        return None

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self._default_id and self._default_id not in self._profiles:
            errs.append(f"default profile '{self._default_id}' not found")
        seen_pairs: set[tuple[str, str]] = set()
        for pid, prof in self._profiles.items():
            if not pid:
                errs.append("empty profile_id")
            if not prof.feature_set_id:
                errs.append(f"profile '{pid}' missing feature_set_id")
            if prof.state not in {"active", "shadow", "disabled"}:
                errs.append(f"profile '{pid}' invalid state '{prof.state}'")
            pair = (prof.feature_set_id, pid)
            if pair in seen_pairs:
                errs.append(f"duplicate profile '{pid}' for feature_set '{prof.feature_set_id}'")
            seen_pairs.add(pair)
        return errs

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self._path,
            "default_profile_id": self._default_id,
            "profiles": [self._profiles[k].to_dict() for k in sorted(self._profiles)],
            "errors": self.validate(),
        }

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "FeatureProfileRegistry":
        reg = cls()
        reg._path = str(path)
        p = Path(path)
        if not p.exists():
            return reg
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Invalid feature profiles file (expected mapping): {path}")
        reg._default_id = str(data.get("default_profile_id") or "").strip() or None
        for entry in data.get("profiles", []) or []:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("profile_id") or "").strip()
            fsid = str(entry.get("feature_set_id") or "").strip()
            if not pid or not fsid:
                continue
            prof = FeatureProfile(
                profile_id=pid,
                feature_set_id=fsid,
                schema_version=(int(entry["schema_version"]) if entry.get("schema_version") is not None else None),
                params=dict(entry.get("params") or {}),
                enabled=bool(entry.get("enabled", True)),
                state=str(entry.get("state", "active") or "active"),
                owner=str(entry.get("owner") or ""),
                notes=str(entry.get("notes") or ""),
            )
            reg.register(prof, make_default=(reg._default_id is None and not reg._profiles))
        return reg


def load_feature_profile_registry(path: str | None = None) -> FeatureProfileRegistry:
    cfg = str(path or os.getenv("HFT_FEATURE_PROFILES_CONFIG", "config/feature_profiles.yaml"))
    return FeatureProfileRegistry.from_file(cfg)
