"""Gate outcomes and the one function that turns them into a verdict.

Every gate check reports one ``Outcome``; ``derive_verdict`` is the only place
that maps a set of outcomes to a research ``Verdict`` (ADR-005, verdict rules
v1). A KILL needs a *powered* failure: a candidate whose blocking checks could
not be measured, or were measured on too little data, is not dead, and a run
whose checks errored has no verdict at all.

Rules v1, applied to the blocking checks only::

    any ERROR, or NOT_APPLICABLE the profile does not defer -> INVALID
    FAIL, powered                                          -> KILL
    INSUFFICIENT_SAMPLE that more days cannot fix          -> INCONCLUSIVE
    INSUFFICIENT_SAMPLE with a reachable required n        -> NEEDS-MORE-DAYS
    FAIL underpowered, or INSUFFICIENT_SAMPLE without n    -> INCONCLUSIVE
    otherwise                                              -> PASS

A FAIL is powered when the check says so (``powered``); otherwise the run's
sample gate (``min_sample_size``) stands in: a FAIL counts as powered only when
that gate passed. Callers that have already established the sample (Gate D runs
only on a Gate C PASS) pass ``powered=True``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

VERDICT_RULES_VERSION = "v1"
SAMPLE_GATE = "min_sample_size"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"


class Verdict(StrEnum):
    PASS = "PASS"
    KILL = "KILL"
    NEEDS_MORE_DAYS = "NEEDS-MORE-DAYS"
    INCONCLUSIVE = "INCONCLUSIVE"
    INVALID = "INVALID"


# Outcomes that can never carry ``passed=True``. NOT_APPLICABLE may carry
# either value: legacy loose-profile gates report a skip as ``passed=True``.
_NOT_PASSED = frozenset({Outcome.FAIL, Outcome.INSUFFICIENT_SAMPLE, Outcome.ERROR})


def resolve_outcome(passed: bool | None, outcome: Outcome | str | None) -> Outcome:
    """Return the outcome a check reports, deriving it from ``passed`` when absent.

    Raises ``ValueError`` when ``passed`` and ``outcome`` contradict each other,
    so a check can never report PASS while marked failed, or the reverse.
    """
    if outcome is None:
        if passed is None:
            return Outcome.ERROR
        return Outcome.PASS if passed else Outcome.FAIL
    resolved = Outcome(outcome)
    if passed is not None:
        if resolved is Outcome.PASS and not passed:
            raise ValueError("outcome=pass contradicts passed=False")
        if resolved in _NOT_PASSED and passed:
            raise ValueError(f"outcome={resolved.value} contradicts passed=True")
    return resolved


def check_entry(
    name: str,
    outcome: Outcome,
    *,
    metrics: Mapping[str, Any] | None = None,
    details: str = "",
    powered: bool | None = None,
) -> dict[str, Any]:
    """Build a check dict in the shape Gate C stores for its sub-gates."""
    entry: dict[str, Any] = {
        "name": name,
        "passed": outcome is Outcome.PASS,
        "outcome": outcome.value,
        "metrics": dict(metrics or {}),
        "details": details,
    }
    if powered is not None:
        entry["powered"] = bool(powered)
    return entry


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()
    obligations: tuple[tuple[str, str], ...] = ()
    required_n: int | None = None
    rules_version: str = VERDICT_RULES_VERSION
    outcomes: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "rules_version": self.rules_version,
            "reasons": list(self.reasons),
            "failing": list(self.failing),
            "obligations": [{"gate": gate, "stage": stage} for gate, stage in self.obligations],
            "required_n": self.required_n,
            "outcomes": dict(self.outcomes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> VerdictResult:
        """Rebuild a stored verdict; raises ``ValueError`` on an unknown verdict."""
        obligations = tuple(
            (str(item.get("gate", "")), str(item.get("stage", "")))
            for item in payload.get("obligations") or ()
            if isinstance(item, Mapping)
        )
        required_n = payload.get("required_n")
        return cls(
            verdict=Verdict(str(payload.get("verdict", ""))),
            reasons=tuple(str(r) for r in payload.get("reasons") or ()),
            failing=tuple(str(r) for r in payload.get("failing") or ()),
            obligations=obligations,
            required_n=int(required_n) if required_n is not None else None,
            rules_version=str(payload.get("rules_version", VERDICT_RULES_VERSION)),
            outcomes={str(k): str(v) for k, v in (payload.get("outcomes") or {}).items()},
        )


def _entry_outcome(entry: Mapping[str, Any]) -> Outcome:
    if entry.get("error"):
        return Outcome.ERROR
    passed = entry.get("passed")
    return resolve_outcome(None if passed is None else bool(passed), entry.get("outcome"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _classify(
    outcome: Outcome, entry: Mapping[str, Any], *, deferred: bool, sample_powered: bool
) -> tuple[str, int | None]:
    """Bucket one non-passing blocking check; returns (bucket, required_n)."""
    if outcome is Outcome.NOT_APPLICABLE:
        return ("deferred" if deferred else "not_deferrable"), None
    if outcome is Outcome.ERROR:
        return "error", None
    if outcome is Outcome.FAIL:
        entry_powered = entry.get("powered")
        is_powered = sample_powered if entry_powered is None else bool(entry_powered)
        return ("killed" if is_powered else "underpowered"), None
    metrics = entry.get("metrics") or {}
    required_n = _int_or_none(metrics.get("required_n"))
    if metrics.get("reachable") is False:
        return "unreachable", None
    if required_n is None:
        return "underpowered", None
    return "more_days", required_n


def derive_verdict(
    entries: Iterable[Mapping[str, Any]],
    *,
    blocking: Collection[str],
    deferrable: Mapping[str, str] | None = None,
    powered: bool | None = None,
) -> VerdictResult:
    """Map check outcomes to one verdict under rules v1.

    ``entries`` are check dicts (``name``, ``outcome`` or ``passed``,
    ``metrics``, optional ``powered``). Only names in ``blocking`` decide the
    verdict; the rest are recorded in ``outcomes`` and otherwise ignored.
    ``deferrable`` maps a gate name to the later stage that owns it when it is
    NOT_APPLICABLE here.
    """
    deferrable = dict(deferrable or {})
    blocking_names = set(blocking)
    resolved: list[tuple[str, Outcome, Mapping[str, Any]]] = []
    outcomes: dict[str, str] = {}
    for entry in entries:
        name = str(entry.get("name", ""))
        outcome = _entry_outcome(entry)
        outcomes[name] = outcome.value
        resolved.append((name, outcome, entry))

    sample_powered = outcomes.get(SAMPLE_GATE) == Outcome.PASS.value if powered is None else bool(powered)

    buckets: dict[str, list[str]] = {}
    obligations: list[tuple[str, str]] = []
    failing: list[str] = []
    required: list[int] = []
    for name, outcome, entry in resolved:
        if name not in blocking_names or outcome is Outcome.PASS:
            continue
        bucket, required_n = _classify(outcome, entry, deferred=name in deferrable, sample_powered=sample_powered)
        if bucket == "deferred":
            obligations.append((name, deferrable[name]))
            continue
        failing.append(name)
        buckets.setdefault(bucket, []).append(name)
        if required_n is not None:
            required.append(required_n)

    def _result(verdict: Verdict, reasons: list[str], required_n: int | None = None) -> VerdictResult:
        return VerdictResult(
            verdict=verdict,
            reasons=tuple(reasons),
            failing=tuple(failing),
            obligations=tuple(obligations),
            required_n=required_n,
            outcomes=outcomes,
        )

    invalid = [f"{n}:error" for n in buckets.get("error", [])] + [
        f"{n}:not_applicable_not_deferrable" for n in buckets.get("not_deferrable", [])
    ]
    if invalid:
        return _result(Verdict.INVALID, invalid)
    if "killed" in buckets:
        return _result(Verdict.KILL, buckets["killed"])
    if "unreachable" in buckets:
        return _result(Verdict.INCONCLUSIVE, [f"{n}:unreachable_sample" for n in buckets["unreachable"]])
    if "more_days" in buckets:
        return _result(Verdict.NEEDS_MORE_DAYS, buckets["more_days"], max(required))
    if "underpowered" in buckets:
        return _result(Verdict.INCONCLUSIVE, [f"{n}:underpowered" for n in buckets["underpowered"]])
    return _result(Verdict.PASS, [])
