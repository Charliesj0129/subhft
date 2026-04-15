"""HealingPlaybook — YAML-driven fault-to-repair-step lookup."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from hft_platform.healing.fault import FaultEvent, FaultSeverity, RiskLevel

logger = structlog.get_logger("healing.playbook")

_SEVERITY_MAP: dict[str, FaultSeverity] = {
    "transient": FaultSeverity.TRANSIENT,
    "degraded": FaultSeverity.DEGRADED,
    "impaired": FaultSeverity.IMPAIRED,
    "critical": FaultSeverity.CRITICAL,
}

_RISK_MAP: dict[str, RiskLevel] = {
    "auto": RiskLevel.AUTO,
    "confirm": RiskLevel.CONFIRM,
}


@dataclass(slots=True, frozen=True)
class PlaybookAction:
    name: str
    risk: RiskLevel
    timeout_s: float
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlaybookEntry:
    name: str
    match_category: str
    match_description_contains: str | None
    match_min_severity: FaultSeverity | None
    actions: list[PlaybookAction]
    cooldown_s: float
    max_retries: int


class HealingPlaybook:
    __slots__ = ("_playbooks", "_last_used_ns")

    def __init__(self, config_path: Path | None = None) -> None:
        self._playbooks: list[PlaybookEntry] = []
        self._last_used_ns: dict[str, int] = {}
        if config_path is not None and config_path.exists():
            self._load(config_path)

    def _load(self, path: Path) -> None:
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            for name, cfg in raw.get("playbooks", {}).items():
                match = cfg.get("match", {})
                actions = []
                for a in cfg.get("actions", []):
                    actions.append(PlaybookAction(
                        name=a["name"],
                        risk=_RISK_MAP.get(a.get("risk", "auto"), RiskLevel.AUTO),
                        timeout_s=float(a.get("timeout_s", 30)),
                        params=a.get("params", {}),
                    ))
                min_sev_str = match.get("min_severity")
                self._playbooks.append(PlaybookEntry(
                    name=name,
                    match_category=match.get("category", ""),
                    match_description_contains=match.get("description_contains"),
                    match_min_severity=_SEVERITY_MAP.get(min_sev_str) if min_sev_str else None,
                    actions=actions,
                    cooldown_s=float(cfg.get("cooldown_s", 60)),
                    max_retries=int(cfg.get("max_retries", 3)),
                ))
            logger.info("healing_playbook_loaded", count=len(self._playbooks))
        except Exception as exc:
            logger.error("healing_playbook_load_failed", error=str(exc))

    def find_match(self, fault: FaultEvent) -> PlaybookEntry | None:
        """Return the first matching playbook entry that is not in cooldown."""
        for entry in self._playbooks:
            if not self._matches(entry, fault):
                continue
            last = self._last_used_ns.get(entry.name, 0)
            cooldown_ns = int(entry.cooldown_s * 1_000_000_000)
            if last > 0 and (fault.ts_ns - last) < cooldown_ns:
                continue
            return entry
        return None

    @staticmethod
    def _matches(entry: PlaybookEntry, fault: FaultEvent) -> bool:
        """Return True if the fault satisfies all match criteria in the entry."""
        if entry.match_category and fault.category.value != entry.match_category:
            return False
        if entry.match_description_contains and entry.match_description_contains not in fault.description:
            return False
        if entry.match_min_severity is not None and fault.severity < entry.match_min_severity:
            return False
        return True

    def mark_used(self, playbook_name: str, ts_ns: int) -> None:
        """Record the timestamp at which a playbook was last dispatched."""
        self._last_used_ns[playbook_name] = ts_ns
