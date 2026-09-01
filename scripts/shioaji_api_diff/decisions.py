"""The upgrade decision behind every Shioaji release newer than the pin.

``watch`` answers "what is out there". It cannot answer "and what did we decide
about it", and the difference matters: a captured API surface is evidence, not a
decision. Once a surface is captured the watcher goes quiet, so the way to
silence a release you have not thought about is exactly the same as the way to
silence one you have -- run the capture. That turns the weekly job into a
reminder to run a command rather than a reminder to make a call.

This module is the missing half. ``docs/runbooks/shioaji-release-decisions.yaml``
records, per version, one of three verdicts with the evidence it rests on, and
this loader refuses a ledger that does not hold together:

* a decision must cite a committed diff golden, and that diff must **agree with
  it** -- ``ADOPT`` is refused when the diff names a platform-breaking change,
  ``BLOCKED`` is refused when it names none. Both sides of that crossing are
  generated from the same captured surfaces, so a claim that contradicts its own
  evidence is a mistake, never a judgement call.
* ``DEFER`` must carry ``revisit_after``. A deferral with no expiry is
  indistinguishable from having forgotten, and the whole point of a scheduled
  watcher is that "not now" comes back.
* unknown keys are rejected. A misspelled ``revist_after`` would otherwise make
  a deferral permanent and silent -- the failure this file exists to prevent.

YAML rather than JSON because the rationale is the payload and humans write it;
``watch`` itself stays stdlib-only so the release check never needs the ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .paths import REPO_ROOT

LEDGER_PATH = REPO_ROOT / "docs" / "runbooks" / "shioaji-release-decisions.yaml"

ADOPT = "ADOPT"  # move the pin -- the diff shows nothing the platform uses breaks
DEFER = "DEFER"  # not now, and here is the date it comes back
BLOCKED = "BLOCKED"  # the diff names a platform-breaking change; adapter work first
VERDICTS = (ADOPT, DEFER, BLOCKED)

CURRENT = "CURRENT"
EXPIRED = "EXPIRED"

_REQUIRED = ("version", "verdict", "decided_on", "rationale", "evidence")
_OPTIONAL = ("revisit_after", "blocking_symbols")


class DecisionLedgerError(ValueError):
    """The ledger is malformed, or a decision contradicts its own evidence."""


@dataclass(frozen=True)
class Decision:
    """One recorded verdict about one published release."""

    version: str
    verdict: str
    decided_on: date
    rationale: str
    evidence: str
    revisit_after: date | None = None
    blocking_symbols: tuple[str, ...] = ()

    def status(self, today: date) -> str:
        """``EXPIRED`` once a deferral's revisit date has passed, else ``CURRENT``."""
        if self.revisit_after is not None and today > self.revisit_after:
            return EXPIRED
        return CURRENT


def _as_date(value: Any, field: str, version: str) -> date:
    # yaml.safe_load already yields ``datetime.date`` for a bare ISO date; a
    # quoted one arrives as str. Accept both, reject anything else rather than
    # coercing -- a date that parses wrongly silently moves a revisit.
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise DecisionLedgerError(f"{version}: {field} is not an ISO date: {value!r}") from exc
    raise DecisionLedgerError(f"{version}: {field} is not a date: {value!r}")


def _breaking_platform(evidence: Path) -> tuple[int, set[str]]:
    """``(count, qualnames)`` of the platform-breaking changes a diff golden names."""
    doc = json.loads(evidence.read_text(encoding="utf-8"))
    names = {
        str(change.get("qualname"))
        for change in doc.get("changes") or []
        if change.get("classification") == "BREAKING" and change.get("platform_used")
    }
    return int((doc.get("counts") or {}).get("breaking_platform", 0)), names


def _check_against_evidence(decision: Decision, pin: str, repo_root: Path) -> None:
    # The evidence must describe *the upgrade being decided* -- pin to candidate.
    # A consecutive hop like 1.7.2 -> 1.7.3 is SAFE on its own while the move
    # from the pin is BLOCKED, so citing one would let a verdict rest on a diff
    # nobody would ever perform.
    expected = f"diff_{pin}_to_{decision.version}.json"
    if Path(decision.evidence).name != expected:
        raise DecisionLedgerError(
            f"{decision.version}: evidence must be the pin-to-candidate diff {expected}, "
            f"got {Path(decision.evidence).name}"
        )
    evidence = repo_root / decision.evidence
    if not evidence.is_file():
        raise DecisionLedgerError(f"{decision.version}: evidence does not exist: {decision.evidence}")
    count, names = _breaking_platform(evidence)
    if decision.verdict == ADOPT and count:
        raise DecisionLedgerError(
            f"{decision.version}: ADOPT contradicts its evidence -- "
            f"{decision.evidence} names {count} platform-breaking change(s): {sorted(names)}"
        )
    if decision.verdict == BLOCKED:
        if not count:
            raise DecisionLedgerError(
                f"{decision.version}: BLOCKED contradicts its evidence -- "
                f"{decision.evidence} names no platform-breaking change"
            )
        unknown = sorted(set(decision.blocking_symbols) - names)
        if unknown:
            raise DecisionLedgerError(f"{decision.version}: blocking_symbols not in {decision.evidence}: {unknown}")


def _decision_from_entry(entry: Any, pin: str, repo_root: Path) -> Decision:
    if not isinstance(entry, dict):
        raise DecisionLedgerError(f"each decision must be a mapping, got {type(entry).__name__}")
    version = str(entry.get("version", "")).strip()
    if not version:
        raise DecisionLedgerError("a decision has no version")

    unknown = sorted(set(entry) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        # Not pedantry: a misspelled key is silently dropped by a permissive
        # loader, and the one that matters (`revisit_after`) fails open.
        raise DecisionLedgerError(f"{version}: unknown key(s) {unknown}")
    missing = sorted(field for field in _REQUIRED if entry.get(field) in (None, ""))
    if missing:
        raise DecisionLedgerError(f"{version}: missing {missing}")

    verdict = str(entry["verdict"]).strip()
    if verdict not in VERDICTS:
        raise DecisionLedgerError(f"{version}: verdict must be one of {list(VERDICTS)}, got {verdict!r}")

    rationale = str(entry["rationale"]).strip()
    if not rationale:
        raise DecisionLedgerError(f"{version}: rationale is empty")

    decided_on = _as_date(entry["decided_on"], "decided_on", version)
    revisit_after = None
    if entry.get("revisit_after") is not None:
        revisit_after = _as_date(entry["revisit_after"], "revisit_after", version)

    if verdict == DEFER and revisit_after is None:
        raise DecisionLedgerError(
            f"{version}: DEFER must carry revisit_after -- a deferral with no expiry is a silent drop"
        )
    if verdict != DEFER and revisit_after is not None:
        raise DecisionLedgerError(f"{version}: revisit_after is only meaningful on DEFER")
    if revisit_after is not None and revisit_after <= decided_on:
        raise DecisionLedgerError(f"{version}: revisit_after ({revisit_after}) must be after decided_on ({decided_on})")

    raw_symbols = entry.get("blocking_symbols") or []
    if not isinstance(raw_symbols, list):
        raise DecisionLedgerError(f"{version}: blocking_symbols must be a list")
    blocking_symbols = tuple(str(s).strip() for s in raw_symbols)
    if verdict == BLOCKED and not blocking_symbols:
        raise DecisionLedgerError(f"{version}: BLOCKED must name the blocking_symbols it rests on")
    if verdict != BLOCKED and blocking_symbols:
        raise DecisionLedgerError(f"{version}: blocking_symbols is only meaningful on BLOCKED")

    decision = Decision(
        version=version,
        verdict=verdict,
        decided_on=decided_on,
        rationale=rationale,
        evidence=str(entry["evidence"]).strip(),
        revisit_after=revisit_after,
        blocking_symbols=blocking_symbols,
    )
    _check_against_evidence(decision, pin, repo_root)
    return decision


def load_decisions(path: Path | None = None, *, repo_root: Path | None = None) -> tuple[str, dict[str, Decision]]:
    """``(pin the ledger was written against, decisions by version)``.

    The pin is returned rather than checked here so the caller can compare it
    against ``pyproject.toml`` and say which one moved. A ledger written against
    a different pin is not stale detail -- every verdict in it was reached
    relative to that pin, so none of them still apply.
    """
    ledger_path = LEDGER_PATH if path is None else Path(path)
    root = REPO_ROOT if repo_root is None else Path(repo_root)
    if not ledger_path.is_file():
        raise DecisionLedgerError(f"decision ledger not found: {ledger_path}")
    data = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DecisionLedgerError(f"{ledger_path}: top level must be a mapping")

    pin = str(data.get("pin", "")).strip()
    if not pin:
        raise DecisionLedgerError(
            f"{ledger_path}: no `pin` -- decisions are meaningless without the version they are relative to"
        )

    entries = data.get("decisions")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise DecisionLedgerError(f"{ledger_path}: `decisions` must be a list")

    out: dict[str, Decision] = {}
    for entry in entries:
        decision = _decision_from_entry(entry, pin, root)
        if decision.version in out:
            raise DecisionLedgerError(f"{decision.version}: decided twice -- which one is current?")
        out[decision.version] = decision
    return pin, out
