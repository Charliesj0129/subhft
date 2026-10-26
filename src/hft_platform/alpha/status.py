"""Alpha status derived from the verdict ledger (ADR-005 section 6).

Validation and promotion never rewrite ``impl.py``; an alpha's lifecycle
status is read from ``ledger/verdicts.jsonl`` under the evidence root.

Derivation rules v1, over one alpha's events in ledger order:

- An event whose ``verdict`` is INVALID is ignored: the run produced no verdict.
- Any KILL verdict makes the alpha KILLED. A kill is not undone by a later
  run; retesting a killed hypothesis is a new candidate.
- Otherwise the status climbs the ladder gate_a -> gate_e and stops at the
  first stage whose latest event failed, is missing, or is older than the
  latest event of the stage below it (a pass on evidence the newer lower-stage
  run has replaced does not count). No passed stage is DRAFT.
- gate_f has no status of its own; PRODUCTION is set by the manual config PR.

An alpha with no ledger events keeps the status its manifest declares.
Manifest statuses the ledger never derives (PRODUCTION, DEPRECATED, KILLED)
are kept over a ladder status; a ledger KILL wins over all of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from hft_platform.alpha.evidence import EvidenceWriter, evidence_root
from hft_platform.alpha.outcomes import Verdict
from hft_platform.contracts.alpha import AlphaStatus

STATUS_RULES_VERSION = "status_rules.v1"

_LADDER: tuple[tuple[str, AlphaStatus], ...] = (
    ("gate_a", AlphaStatus.GATE_A),
    ("gate_b", AlphaStatus.GATE_B),
    ("gate_c", AlphaStatus.GATE_C),
    ("gate_d", AlphaStatus.GATE_D),
    ("gate_e", AlphaStatus.GATE_E),
)
_MANUAL_STATUSES = frozenset({AlphaStatus.PRODUCTION, AlphaStatus.DEPRECATED, AlphaStatus.KILLED})


class StatusSource(StrEnum):
    LEDGER = "ledger"
    MANIFEST = "manifest"


@dataclass(frozen=True, slots=True)
class EffectiveStatus:
    status: AlphaStatus
    source: StatusSource


def derive_status(events: Iterable[Mapping[str, Any]]) -> AlphaStatus | None:
    """The status one alpha's ledger events imply, or None when there are none."""
    latest: dict[str, tuple[int, bool]] = {}
    seen = False
    for index, event in enumerate(events):
        seen = True
        verdict = event.get("verdict")
        if verdict == Verdict.INVALID.value:
            continue
        if verdict == Verdict.KILL.value:
            return AlphaStatus.KILLED
        latest[str(event.get("stage"))] = (index, event.get("passed") is True)
    if not seen:
        return None
    status = AlphaStatus.DRAFT
    below = -1
    for stage, stage_status in _LADDER:
        entry = latest.get(stage)
        if entry is None or not entry[1] or entry[0] < below:
            break
        status, below = stage_status, entry[0]
    return status


def effective_status(declared: AlphaStatus, events: Iterable[Mapping[str, Any]]) -> EffectiveStatus:
    """The ledger-derived status when the alpha has events, else the declared one."""
    derived = derive_status(events)
    if derived is None:
        return EffectiveStatus(declared, StatusSource.MANIFEST)
    if derived is not AlphaStatus.KILLED and declared in _MANUAL_STATUSES:
        return EffectiveStatus(declared, StatusSource.MANIFEST)
    return EffectiveStatus(derived, StatusSource.LEDGER)


def ledger_events_by_alpha(root: str | Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Ledger events grouped by alpha, in ledger order.

    ``root`` defaults to the evidence root (the default location when unset).
    A missing ledger is empty; a malformed one raises ``ValueError``.
    """
    writer = EvidenceWriter(root if root is not None else evidence_root())
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in writer.read_verdicts():
        grouped.setdefault(str(event["alpha_id"]), []).append(event)
    return grouped
