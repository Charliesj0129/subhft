"""Canonical intent log for the replay-parity gate.

`ReplayedIntentLog` collects emitted intents and produces a canonical record
list + a deterministic stream digest for parity comparison. Canonicalization
and hashing are delegated to the single source of truth in
:mod:`hft_platform.replay.intent_diff` so the live-load path
(``cli_runner._row_to_canonical``) and the replay path cannot drift.

The stable hash excludes generated ids and the wall-clock emission timestamp
(``local_ts``) so the same logical decision hashes identically in live and
replay; it includes the event source timestamp (``source_ts``) as the
input-locating key. See ``intent_diff.HASH_FIELDS``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hft_platform.replay.intent_diff import (
    canonicalize_intent,
    stable_intent_hash,
)

# Back-compat alias: older modules/docstrings reference ``_intent_to_canonical``.
_intent_to_canonical = canonicalize_intent


@dataclass
class _DictIntent:
    """Shim for jsonl-loaded intents (mirrors ``OrderIntent`` fields by name)."""

    intent_id: int = 0
    strategy_id: str = ""
    symbol: str = ""
    intent_type: str = "NEW"
    side: str = "BUY"
    tif: str = "LIMIT"
    price: int = 0
    qty: int = 0
    target_order_id: str = ""
    timestamp_ns: int = 0
    source_ts_ns: int = 0
    decision_price: int = 0
    price_type: str = "LMT"
    reason: str = ""


@dataclass
class ReplayedIntentLog:
    """Append-only buffer of intents with deterministic canonical hashing."""

    intents: list[Any] = field(default_factory=list)
    n_events_processed: int = 0

    def append(self, intent: Any) -> None:
        self.intents.append(intent)

    def n_intents(self) -> int:
        return len(self.intents)

    def canonical_records(self) -> list[dict[str, Any]]:
        return [canonicalize_intent(it) for it in self.intents]

    def hash(self) -> str:
        """SHA-256 of the per-record stable intent hashes, in order.

        Determinism comes from :func:`stable_intent_hash` (sorted-key JSON,
        version-tagged). Two logs whose intents differ only in generated ids
        or wall-clock emission timestamps produce an identical digest.
        """
        h = hashlib.sha256()
        for rec in self.canonical_records():
            h.update(stable_intent_hash(rec).encode("ascii"))
            h.update(b"\n")
        return h.hexdigest()

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "ReplayedIntentLog":
        """Load a canonical-record JSONL file.

        Tolerates blank lines and forward-compat extra keys. Legacy fixtures
        that store ``timestamp_us`` (microseconds, the pre-v2 schema) are
        promoted to ``local_ts`` — a reporting-only field, so the stream
        digest is unaffected. Records are stored as canonical dicts;
        :func:`canonicalize_intent` is idempotent on them.
        """
        log = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "timestamp_us" in d and "local_ts" not in d:
                d["local_ts"] = int(d.pop("timestamp_us"))
            log.intents.append(canonicalize_intent(d))
        return log
