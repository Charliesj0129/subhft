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


@dataclass(frozen=True)
class ReplayParityReport:
    match_pct: float
    n_compared: int
    first_divergence_idx: int | None
    divergence_histogram: dict[str, int]
    evidence_path: str
    harness_version: str = "slice-c.v1"


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
        n_match = 0
        for i in range(n_compared):
            a = self.live[i] if i < len(self.live) else None
            b = self.replayed[i] if i < len(self.replayed) else None
            if a is None or b is None:
                hist["__missing__"] = hist.get("__missing__", 0) + 1
                if first_div is None:
                    first_div = i
                continue
            diffs = [k for k in a if a[k] != b.get(k)]
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
        )
