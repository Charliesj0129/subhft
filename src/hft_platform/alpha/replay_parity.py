"""IntentDiff + ReplayParityReport — Slice C task 6.

Compares two canonical intent streams (live + replayed) and produces a
:class:`ReplayParityReport` describing match percentage, first divergence
index, and a per-field divergence histogram. Consumed by the
ReplayParityGate sub-gate (Slice C task 8) and Gate D replay_parity_audit
(task 10).

The canonical intent dict schema is produced by
``hft_platform.alpha.intent_log._intent_to_canonical`` (Slice C task 5).
This module is schema-agnostic: it diffs whatever keys exist in the live
side. Each entry is treated as one observation; per-key inequality counts
toward the divergence histogram, and a length mismatch is bucketed under
the special ``__missing__`` key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Sentinel distinguishing "key absent" from "key present with value None".
# An asymmetric schema (a field present on exactly one side) is itself a
# divergence worth surfacing, so absence must compare unequal to any real
# value — including a literal None.
_MISSING = object()


@dataclass(frozen=True)
class ReplayParityReport:
    match_pct: float
    n_compared: int
    first_divergence_idx: int | None
    divergence_histogram: dict[str, int]
    evidence_path: str
    harness_version: str = "slice-c.v1"
    # Union of every key observed across the compared records. A field that
    # never appears here was NOT checked by this run — so a 100% match_pct on
    # a stream missing e.g. ``session_phase`` must not be read as "session
    # parity verified". Lets the gate report coverage honestly rather than
    # treating absence as agreement.
    observed_fields: tuple[str, ...] = ()


@dataclass
class IntentDiff:
    live: list[dict[str, Any]]
    replayed: list[dict[str, Any]]
    evidence_path: str = ""

    def compute(self) -> ReplayParityReport:
        n_compared = max(len(self.live), len(self.replayed))
        if n_compared == 0:
            return ReplayParityReport(100.0, 0, None, {}, self.evidence_path)
        first_div: int | None = None
        hist: dict[str, int] = {}
        observed: set[str] = set()
        n_match = 0
        for i in range(n_compared):
            a = self.live[i] if i < len(self.live) else None
            b = self.replayed[i] if i < len(self.replayed) else None
            if a is not None:
                observed.update(a.keys())
            if b is not None:
                observed.update(b.keys())
            if a is None or b is None:
                hist["__missing__"] = hist.get("__missing__", 0) + 1
                if first_div is None:
                    first_div = i
                continue
            # Diff the UNION of keys, not just live's. Iterating only `a`
            # silently ignores any field the replay emits that live lacks
            # (e.g. a renamed/extra intent field), which would inflate
            # match_pct and let a divergent strategy pass the parity gate.
            # The _MISSING sentinel makes a one-sided field compare unequal
            # to a present value, including None.
            diffs = [k for k in a.keys() | b.keys() if a.get(k, _MISSING) != b.get(k, _MISSING)]
            if not diffs:
                n_match += 1
                continue
            for k in diffs:
                hist[k] = hist.get(k, 0) + 1
            if first_div is None:
                first_div = i
        match_pct = (n_match / n_compared) * 100.0
        return ReplayParityReport(
            match_pct=match_pct,
            n_compared=n_compared,
            first_divergence_idx=first_div,
            divergence_histogram=dict(hist),
            evidence_path=self.evidence_path,
            observed_fields=tuple(sorted(observed)),
        )
