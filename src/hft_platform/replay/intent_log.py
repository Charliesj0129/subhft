"""Canonical intent log for Slice C replay-parity gate.

`ReplayedIntentLog` collects emitted intents and produces a canonical
``bytes`` form for hashing. The same canonical form is used to hash a
"live" intent stream loaded from ``hft.order_intents`` (Task 14) or a
synthetic fixture (Task 7). Microsecond-rounded timestamps prevent
sub-microsecond scheduler jitter from breaking parity, while keeping the
parity bar tight enough to catch the R47-OE1 cancel-path divergence
(which is whole-event-shape, not timing).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


_OPTIONAL_PARITY_FIELDS: tuple[str, ...] = (
    # Round 15: goal §7 parity checks for session filter / risk filter /
    # force-flat consistency.  Emitted ONLY when present on the source
    # intent so historical fixtures (which never had these attrs) hash
    # identically — adding them unconditionally would invalidate every
    # previously-stored canonical digest.
    "session_phase",
    "risk_filter_active",
    "force_flat_triggered",
)


def _intent_to_canonical(intent: Any) -> dict[str, Any]:
    """Project an intent (real ``OrderIntent`` or ``_DictIntent`` shim) onto
    the canonical schema. Volatile/runtime-uniqued fields (``trace_id``,
    ``idempotency_key``, ``ttl_ns``, ``reason``, ``ingest_ts``,
    ``source_ts_ns``) are intentionally excluded.

    The three optional parity fields (``session_phase``,
    ``risk_filter_active``, ``force_flat_triggered``) are included only
    when the source intent carries a non-``None`` value, keeping
    historical hashes stable for intents that predate Round 15.
    """
    record: dict[str, Any] = {
        "intent_id": int(getattr(intent, "intent_id", 0)),
        "strategy_id": str(getattr(intent, "strategy_id", "")),
        "symbol": str(getattr(intent, "symbol", "")),
        "intent_type": getattr(intent.intent_type, "name", str(intent.intent_type)),
        "side": getattr(intent.side, "name", str(intent.side)),
        "tif": getattr(intent.tif, "name", str(intent.tif)),
        "price": int(getattr(intent, "price", 0)),
        "qty": int(getattr(intent, "qty", 0)),
        "target_order_id": str(getattr(intent, "target_order_id", "") or ""),
        "timestamp_us": int(getattr(intent, "timestamp_ns", 0)) // 1000,
        "decision_price": int(getattr(intent, "decision_price", 0)),
        "price_type": str(getattr(intent, "price_type", "LMT")),
    }
    for name in _OPTIONAL_PARITY_FIELDS:
        value = getattr(intent, name, None)
        if value is None:
            continue
        if name == "session_phase":
            record[name] = str(value)
        else:
            record[name] = bool(value)
    return record


@dataclass
class _DictIntent:
    """Shim for jsonl-loaded intents (mirrors ``OrderIntent`` fields by name).

    Canonical fixtures stored on disk use ``timestamp_us`` (microseconds);
    :py:meth:`ReplayedIntentLog.from_jsonl` promotes it to ``timestamp_ns``
    before instantiation so the round-trip lands in the same microsecond
    bucket: ``timestamp_us == (timestamp_us * 1000) // 1000``.
    """

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
    decision_price: int = 0
    price_type: str = "LMT"
    # Round 15 optional parity fields (goal §7).  ``None`` is the
    # "field absent" sentinel — it tells ``_intent_to_canonical`` to
    # skip emitting the key so historical hashes stay stable.
    session_phase: str | None = None
    risk_filter_active: bool | None = None
    force_flat_triggered: bool | None = None


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
        return [_intent_to_canonical(it) for it in self.intents]

    def hash(self) -> str:
        """SHA-256 of the JSONL-encoded canonical records.

        Determinism: ``json.dumps(..., sort_keys=True, separators=(",", ":"))``
        yields a stable byte sequence across processes and Python versions.
        """
        h = hashlib.sha256()
        for rec in self.canonical_records():
            h.update(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "ReplayedIntentLog":
        """Load a canonical-record JSONL file produced by ``canonical_records``.

        Tolerates blank lines and forward-compat extra keys (silently dropped).
        Promotes ``timestamp_us`` -> ``timestamp_ns`` so a round-trip through
        :py:meth:`hash` produces an identical digest.
        """
        log = cls()
        allowed = {f.name for f in fields(_DictIntent)}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            # Canonical fixtures store timestamp_us (microseconds, rounded).
            # Promote it to timestamp_ns so _intent_to_canonical(...) round-trips
            # to the same bucket: timestamp_us = (timestamp_us * 1000) // 1000.
            if "timestamp_us" in d and "timestamp_ns" not in d:
                d["timestamp_ns"] = int(d.pop("timestamp_us")) * 1000
            # Drop any keys _DictIntent doesn't accept (defensive against
            # canonical-schema additions in future slices).
            d = {k: v for k, v in d.items() if k in allowed}
            log.intents.append(_DictIntent(**d))
        return log
