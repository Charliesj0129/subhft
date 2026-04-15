"""Fault event data models for the self-healing framework."""
from __future__ import annotations

import enum
from dataclasses import dataclass


class FaultCategory(enum.StrEnum):
    FEED = "feed"
    BROKER = "broker"
    INFRA = "infra"
    POSITION = "position"
    CONTRACT = "contract"
    EXECUTION = "execution"


class FaultSeverity(enum.IntEnum):
    TRANSIENT = 0
    DEGRADED = 1
    IMPAIRED = 2
    CRITICAL = 3


class RiskLevel(enum.IntEnum):
    AUTO = 0
    CONFIRM = 1


@dataclass(slots=True, frozen=True)
class FaultEvent:
    fault_id: str
    category: FaultCategory
    severity: FaultSeverity
    source: str
    description: str
    ts_ns: int
    context: dict | None
