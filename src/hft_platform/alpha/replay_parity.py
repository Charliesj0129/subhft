"""IntentDiff + ReplayParityReport — replay-parity gate.

Thin adapter over the shared diff engine
(:mod:`hft_platform.replay.intent_diff`). ``IntentDiff.compute`` delegates to
:func:`diff_intent_streams` so the gate, the CLI runner, and the daily ops job
share one comparison; there is no second diff implementation.

``ReplayParityReport`` keeps the historical ``match_pct`` /
``first_divergence_idx`` / ``divergence_histogram`` surface (consumed by the
ReplayParityGate sub-gate and Gate D) and adds the strict ``ok`` flag,
``mismatch_type`` and the localizable ``first_divergence`` payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hft_platform.replay.intent_diff import (
    HASH_VERSION,
    INTENT_SCHEMA_VERSION,
    diff_intent_streams,
)


@dataclass(frozen=True)
class ReplayParityReport:
    match_pct: float
    n_compared: int
    first_divergence_idx: int | None
    divergence_histogram: dict[str, int]
    evidence_path: str
    ok: bool = True
    mismatch_type: str | None = None
    first_divergence: dict[str, Any] | None = None
    path_pair: str = "live_vs_replay"
    intent_schema_version: str = INTENT_SCHEMA_VERSION
    hash_version: str = HASH_VERSION
    harness_version: str = "slice-c.v1"


@dataclass
class IntentDiff:
    live: list[dict[str, Any]]
    replayed: list[dict[str, Any]]
    evidence_path: str = ""
    path_pair: str = "live_vs_replay"
    expect_nonempty: bool = True

    def compute(self) -> ReplayParityReport:
        result = diff_intent_streams(
            self.live,
            self.replayed,
            path_pair=self.path_pair,
            expect_nonempty=self.expect_nonempty,
        )
        fd = result.first_divergence
        return ReplayParityReport(
            match_pct=result.match_pct,
            n_compared=result.n_compared,
            first_divergence_idx=fd.event_index if fd is not None else None,
            divergence_histogram=dict(result.divergence_histogram),
            evidence_path=self.evidence_path,
            ok=result.ok,
            mismatch_type=fd.mismatch_type if fd is not None else None,
            first_divergence=fd.to_dict() if fd is not None else None,
            path_pair=result.path_pair,
            intent_schema_version=result.intent_schema_version,
            hash_version=result.hash_version,
        )
