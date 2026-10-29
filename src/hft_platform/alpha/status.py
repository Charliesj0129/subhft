"""Alpha status derived from the verdict ledger (ADR-005 section 6).

Validation and promotion never rewrite ``impl.py``; an alpha's lifecycle
status is read from ``ledger/verdicts.jsonl`` under the evidence root.

Derivation rules v1, over one alpha's events in ledger order:

- An event whose ``verdict`` is INVALID is ignored: the run produced no verdict.
- Any KILL verdict makes the alpha KILLED. A kill is not undone by a later
  run; retesting a killed hypothesis is a new candidate.
- Otherwise the status climbs the ladder gate_a -> gate_e and stops at the
  first stage whose latest event failed, is missing, is older than the latest
  event of the stage below it, or belongs to a different run (``run_id``) than
  that event. A promotion of an older scorecard therefore never counts for a
  newer validation run. No passed stage is DRAFT.
- gate_f has no status of its own; PRODUCTION is set by the manual config PR.

Every event is checked before it is used: a known ``stage``, a boolean
``passed``, a known ``verdict`` if present, ``passed`` true only with a PASS
verdict or none, a string or null ``run_id``. A malformed event raises
``ValueError``; it never yields a status.

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
_STAGES = frozenset(stage for stage, _ in _LADDER) | {"gate_f"}
_VERDICTS = frozenset(v.value for v in Verdict)


class StatusSource(StrEnum):
    LEDGER = "ledger"
    MANIFEST = "manifest"


@dataclass(frozen=True, slots=True)
class EffectiveStatus:
    status: AlphaStatus
    source: StatusSource


def check_event(event: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` unless ``event`` is a well-formed verdict event."""
    alpha_id = event.get("alpha_id")
    if not isinstance(alpha_id, str) or not alpha_id:
        raise ValueError(f"verdict event has no alpha_id: {dict(event)!r}")
    stage = event.get("stage")
    if stage not in _STAGES:
        raise ValueError(f"{alpha_id}: unknown stage {stage!r}")
    passed = event.get("passed")
    if not isinstance(passed, bool):
        raise ValueError(f"{alpha_id} {stage}: passed is {passed!r}, not a boolean")
    verdict = event.get("verdict")
    if verdict is not None and verdict not in _VERDICTS:
        raise ValueError(f"{alpha_id} {stage}: unknown verdict {verdict!r}")
    if passed and verdict not in (None, Verdict.PASS.value):
        raise ValueError(f"{alpha_id} {stage}: passed with verdict {verdict}")
    run_id = event.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError(f"{alpha_id} {stage}: run_id is {run_id!r}")


def derive_status(events: Iterable[Mapping[str, Any]]) -> AlphaStatus | None:
    """The status one alpha's ledger events imply, or None when there are none.

    A malformed event raises ``ValueError``.
    """
    return _derive(list(events))[0]


def status_basis(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The events the derived status rests on.

    Every KILL; else each climbed stage's latest event, and the event that
    stopped the climb when the status would differ without it (a failed
    gate_a that replaced a passed one, but not an old event that a newer run
    left behind). A malformed event raises ``ValueError``.
    """
    events = list(events)
    status, basis, stop = _derive(events)
    if stop is not None and _derive(events[:stop] + events[stop + 1 :])[0] is not status:
        basis = [*basis, stop]
    return [events[i] for i in basis]


def _derive(events: list[Mapping[str, Any]]) -> tuple[AlphaStatus | None, list[int], int | None]:
    """(status, the indices it climbed on, the index of the event that stopped the climb)."""
    latest: dict[str, tuple[int, bool, str | None]] = {}
    kills: list[int] = []
    for index, event in enumerate(events):
        check_event(event)
        verdict = event.get("verdict")
        if verdict == Verdict.INVALID.value:
            continue
        if verdict == Verdict.KILL.value:
            kills.append(index)
        latest[event["stage"]] = (index, event["passed"], event.get("run_id"))
    if not events:
        return None, [], None
    if kills:
        return AlphaStatus.KILLED, kills, None
    status = AlphaStatus.DRAFT
    basis: list[int] = []
    below: tuple[int, bool, str | None] | None = None
    for stage, stage_status in _LADDER:
        entry = latest.get(stage)
        if entry is None:
            return status, basis, None
        if not entry[1] or (below is not None and (entry[0] < below[0] or entry[2] != below[2])):
            return status, basis, entry[0]
        status, below = stage_status, entry
        basis.append(entry[0])
    return status, basis, None


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
    A missing ledger is empty; a malformed line or event raises ``ValueError``.
    """
    writer = EvidenceWriter(root if root is not None else evidence_root())
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in writer.read_verdicts():
        check_event(event)
        grouped.setdefault(event["alpha_id"], []).append(event)
    return grouped
