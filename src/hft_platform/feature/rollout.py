from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True, slots=True)
class FeatureRolloutAssignment:
    feature_set_id: str
    state: str = "active"  # active|shadow|disabled
    active_profile_id: str | None = None
    shadow_profile_id: str | None = None
    prev_active_profile_id: str | None = None
    version: int = 0
    updated_at: str = ""
    actor: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_set_id": self.feature_set_id,
            "state": self.state,
            "active_profile_id": self.active_profile_id,
            "shadow_profile_id": self.shadow_profile_id,
            "prev_active_profile_id": self.prev_active_profile_id,
            "version": int(self.version),
            "updated_at": self.updated_at,
            "actor": self.actor,
            "notes": self.notes,
        }


class FeatureRolloutController:
    """Local, file-backed rollout controller (prototype scope).

    Deep-water gap filler:
    - persists rollout state for feature profiles
    - provides local status/rollback without changing runtime architecture
    - can be read by bootstrap and CLI
    """

    __slots__ = ("_path", "_sets", "_version")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = str(path)
        self._sets: dict[str, FeatureRolloutAssignment] = {}
        self._version = 0

    @property
    def path(self) -> str:
        return self._path

    @property
    def version(self) -> int:
        return int(self._version)

    def get(self, feature_set_id: str) -> FeatureRolloutAssignment | None:
        return self._sets.get(str(feature_set_id))

    def assignments(self) -> tuple[FeatureRolloutAssignment, ...]:
        return tuple(self._sets[k] for k in sorted(self._sets))

    def resolve_profile_id(self, feature_set_id: str) -> str | None:
        a = self.get(feature_set_id)
        if a is None:
            return None
        if a.state == "disabled":
            return None
        if a.state == "shadow":
            return a.shadow_profile_id or a.active_profile_id
        return a.active_profile_id

    def set_assignment(
        self,
        *,
        feature_set_id: str,
        state: str,
        profile_id: str | None = None,
        shadow_profile_id: str | None = None,
        actor: str = "cli",
        notes: str = "",
    ) -> FeatureRolloutAssignment:
        fsid = str(feature_set_id)
        state = str(state).strip().lower()
        if state not in {"active", "shadow", "disabled"}:
            raise ValueError(f"Invalid rollout state: {state}")
        cur = self._sets.get(fsid)
        cur_active = cur.active_profile_id if cur else None
        cur_shadow = cur.shadow_profile_id if cur else None

        next_active = cur_active
        next_shadow = cur_shadow
        prev_active = cur.prev_active_profile_id if cur else None

        if state == "active":
            if not profile_id:
                raise ValueError("profile_id is required for active state")
            if cur_active and cur_active != profile_id:
                prev_active = cur_active
            next_active = str(profile_id)
            if shadow_profile_id is not None:
                next_shadow = str(shadow_profile_id) if shadow_profile_id else None
        elif state == "shadow":
            # shadow can reuse the current active profile as primary if no profile_id specified
            if profile_id:
                next_shadow = str(profile_id)
            elif shadow_profile_id:
                next_shadow = str(shadow_profile_id)
            elif not cur_shadow:
                raise ValueError("profile_id (shadow profile) is required for first shadow rollout")
        else:  # disabled
            # preserve current IDs for rollback/inspection; just mark disabled
            pass

        self._version += 1
        a = FeatureRolloutAssignment(
            feature_set_id=fsid,
            state=state,
            active_profile_id=next_active,
            shadow_profile_id=next_shadow,
            prev_active_profile_id=prev_active,
            version=self._version,
            updated_at=_utc_now_iso(),
            actor=str(actor or ""),
            notes=str(notes or ""),
        )
        self._sets[fsid] = a
        self.save()
        return a

    def rollback(self, *, feature_set_id: str, actor: str = "cli", notes: str = "") -> FeatureRolloutAssignment:
        fsid = str(feature_set_id)
        cur = self._sets.get(fsid)
        if cur is None:
            raise KeyError(f"No rollout assignment for feature_set {fsid!r}")
        if not cur.prev_active_profile_id:
            raise ValueError(f"No previous active profile recorded for feature_set {fsid!r}")
        return self.set_assignment(
            feature_set_id=fsid,
            state="active",
            profile_id=cur.prev_active_profile_id,
            actor=actor,
            notes=notes or "rollback",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self._path,
            "version": int(self._version),
            "updated_at": _utc_now_iso(),
            "sets": [a.to_dict() for a in self.assignments()],
        }

    def save(self) -> None:
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "FeatureRolloutController":
        ctrl = cls(path)
        p = Path(path)
        if not p.exists():
            return ctrl
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return ctrl
        if not isinstance(data, dict):
            return ctrl
        try:
            ctrl._version = int(data.get("version", 0) or 0)
        except Exception:
            ctrl._version = 0
        for entry in data.get("sets", []) or []:
            if not isinstance(entry, dict):
                continue
            fsid = str(entry.get("feature_set_id") or "").strip()
            if not fsid:
                continue
            ctrl._sets[fsid] = FeatureRolloutAssignment(
                feature_set_id=fsid,
                state=str(entry.get("state", "active") or "active"),
                active_profile_id=(
                    str(entry["active_profile_id"]) if entry.get("active_profile_id") not in {None, ""} else None
                ),
                shadow_profile_id=(
                    str(entry["shadow_profile_id"]) if entry.get("shadow_profile_id") not in {None, ""} else None
                ),
                prev_active_profile_id=(
                    str(entry["prev_active_profile_id"])
                    if entry.get("prev_active_profile_id") not in {None, ""}
                    else None
                ),
                version=int(entry.get("version", 0) or 0),
                updated_at=str(entry.get("updated_at") or ""),
                actor=str(entry.get("actor") or ""),
                notes=str(entry.get("notes") or ""),
            )
        return ctrl


def load_feature_rollout_controller(path: str | None = None) -> FeatureRolloutController:
    cfg = str(path or os.getenv("HFT_FEATURE_ROLLOUT_STATE_PATH", "outputs/feature_rollout_state.json"))
    return FeatureRolloutController.from_file(cfg)
