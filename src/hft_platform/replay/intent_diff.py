"""Shared canonical-intent diff engine for the replay-parity gate.

This module is the **single source of truth** for three things that used to
be scattered (and could silently drift) across ``alpha.replay_parity``,
``replay.intent_log`` and ``replay.cli_runner``:

1. :func:`canonicalize_intent` — project any intent representation
   (``OrderIntent``, the jsonl ``_DictIntent`` shim, or a plain dict already
   loaded from ClickHouse) onto one canonical schema.
2. :func:`stable_intent_hash` — a deterministic, cross-process SHA-256 over
   the *decision-determining* subset of the canonical record. It deliberately
   excludes generated ids and wall-clock timestamps so the same logical
   decision hashes identically in live and replay.
3. :func:`diff_intent_streams` — compare an ``expected`` (live/shadow) stream
   against an ``actual`` (replay) stream, **fail closed** on any structural
   problem (empty / missing log / schema skew / ordering / hash mismatch) and
   emit a localizable :class:`FirstDivergence`.

Why this exists: the 2026-04-21 R47-OE1 incident (live -1,722 vs backtest
+7,701) was a whole-intent-shape divergence the gate must catch. The prior
implementation could report 100% parity on *no data*, hashed a generated
``intent_id`` (so an id reshuffle broke parity while a reordering was
invisible), and never localized a divergence beyond an integer index.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# Bump INTENT_SCHEMA_VERSION when the canonical field set changes; bump
# HASH_VERSION when the hashed-subset or serialization changes. Both are
# embedded in the hash payload and the diff report so a stale fixture or a
# cross-version compare is detectable rather than silently mismatching.
INTENT_SCHEMA_VERSION = "v2"
HASH_VERSION = "v1"

# Decision-determining fields — the only ones that feed the stable hash.
# Anything not in here (generated ids, wall-clock ts, reason strings, optional
# context that may exist on one side only) is carried in the canonical record
# for *reporting* but never affects parity.
HASH_FIELDS: tuple[str, ...] = (
    "strategy_id",
    "symbol",
    "side",
    "intent_type",
    "tif",
    "price",
    "qty",
    "price_type",
    "target_order_id",
    "decision_price",
    "source_ts",
)

# Optional context fields: included in the canonical record (and surfaced in
# first_divergence) only when present, never hashed — they may legitimately
# exist on the live side but not the replay side (or vice versa).
_OPTIONAL_CONTEXT_FIELDS: tuple[str, ...] = (
    "source_event_index",
    "feature_set_id",
    "feature_schema_version",
    "session_phase",
    "track_phase",
    "risk_filter_phase",
)


# Required decision fields: a canonical intent missing any of these cannot be
# safely hashed — defaulting them (e.g. ``side`` -> ``"BUY"``, ``qty`` -> 0)
# would let a malformed intent hash-match a real decision and create false
# safety. ``canonicalize_intent`` raises :class:`MissingDecisionField`; the
# diff engine reports ``schema_mismatch`` for directly-constructed streams.
REQUIRED_DECISION_FIELDS: tuple[str, ...] = ("symbol", "side", "price", "qty")

_MISSING = object()


class MissingDecisionField(ValueError):
    """Raised when an intent lacks a required decision field (cannot be hashed)."""


class MismatchType(str, Enum):
    """Taxonomy of parity failures. All are fail-closed (ok=False)."""

    MISSING_EXPECTED_INTENT = "missing_expected_intent"
    UNEXPECTED_ACTUAL_INTENT = "unexpected_actual_intent"
    INTENT_HASH_MISMATCH = "intent_hash_mismatch"
    ORDERING_MISMATCH = "ordering_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    EMPTY_REPLAY = "empty_replay"
    MISSING_INTENT_LOG = "missing_intent_log"


def _get(intent: Any, name: str, default: Any) -> Any:
    """Read ``name`` from a dict key or an object attribute."""
    if isinstance(intent, dict):
        return intent.get(name, default)
    return getattr(intent, name, default)


def _require_field(intent: Any, name: str) -> Any:
    """Return a required decision field or raise — never substitute a default."""
    val = _get(intent, name, _MISSING)
    if val is _MISSING or val is None:
        raise MissingDecisionField(f"intent missing required decision field: {name!r}")
    return val


def _first_missing_required(rec: dict[str, Any]) -> str | None:
    """Return the first required decision field absent/None in a canonical
    record, or ``None`` if all are present. Used by the diff engine to flag a
    directly-constructed stream that bypassed :func:`canonicalize_intent`."""
    for fld in REQUIRED_DECISION_FIELDS:
        if rec.get(fld) is None:
            return fld
    return None


def _enum_name(value: Any, default: str) -> str:
    """Normalize an enum / enum-name / raw value to its stable string name."""
    if value is None:
        return default
    return str(getattr(value, "name", value))


def canonicalize_intent(intent: Any) -> dict[str, Any]:
    """Project an intent onto the canonical schema (``INTENT_SCHEMA_VERSION``).

    Idempotent: feeding the output of this function back in reproduces it, so
    a jsonl round-trip of canonical records is stable.

    Accepts an ``OrderIntent`` (enum-valued ``side``/``tif``/``intent_type``),
    the ``_DictIntent`` jsonl shim, or a plain dict already in canonical form.

    Hashed decision fields plus reporting-only fields (``intent_id``,
    ``local_ts``, ``reason``) and any present optional context are returned.
    """
    # source_ts: prefer an already-canonical key, else derive from ns.
    source_ts = _get(intent, "source_ts", None)
    if source_ts is None:
        source_ts = int(_get(intent, "source_ts_ns", 0) or 0) // 1000
    # local emission ts (wall-clock; reporting only, NOT hashed).
    local_ts = _get(intent, "local_ts", None)
    if local_ts is None:
        local_ts = int(_get(intent, "timestamp_ns", 0) or 0) // 1000

    rec: dict[str, Any] = {
        "intent_schema_version": str(_get(intent, "intent_schema_version", INTENT_SCHEMA_VERSION)),
        # --- hashed decision fields ---
        # Required fields are read via _require_field so a missing/None value
        # raises instead of silently defaulting to a tradeable value.
        "strategy_id": str(_get(intent, "strategy_id", "") or ""),
        "symbol": str(_require_field(intent, "symbol")),
        "side": _enum_name(_require_field(intent, "side"), "BUY"),
        "intent_type": _enum_name(_get(intent, "intent_type", None), "NEW"),
        "tif": _enum_name(_get(intent, "tif", None), "LIMIT"),
        "price": int(_require_field(intent, "price")),
        "qty": int(_require_field(intent, "qty")),
        "price_type": str(_get(intent, "price_type", "LMT") or "LMT"),
        "target_order_id": str(_get(intent, "target_order_id", "") or ""),
        "decision_price": int(_get(intent, "decision_price", 0) or 0),
        "source_ts": int(source_ts or 0),
        # --- reporting-only (excluded from the stable hash) ---
        "intent_id": int(_get(intent, "intent_id", 0) or 0),
        "local_ts": int(local_ts or 0),
        "reason": str(_get(intent, "reason", "") or ""),
    }
    for fld in _OPTIONAL_CONTEXT_FIELDS:
        val = _get(intent, fld, None)
        if val is not None:
            rec[fld] = val
    return rec


def stable_intent_hash(canonical: dict[str, Any]) -> str:
    """Deterministic SHA-256 over the decision-determining subset.

    * Never uses the Python built-in ``hash()`` (per-process salted).
    * Sorted-key, separator-tight JSON → byte-stable across processes and
      Python versions.
    * Embeds ``HASH_VERSION`` so a hashing-scheme change cannot masquerade as
      a parity match.
    """
    payload = {
        "hash_version": HASH_VERSION,
        "fields": {k: canonical.get(k) for k in HASH_FIELDS},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FirstDivergence:
    """Everything needed to localize the first parity break."""

    path_pair: str
    event_index: int
    mismatch_type: str
    symbol: str | None = None
    source_ts: int | None = None
    local_ts: int | None = None
    strategy_id: str | None = None
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntentDiffResult:
    """Result of one stream comparison. ``ok`` is strict: any divergence → False."""

    ok: bool
    path_pair: str
    n_expected: int
    n_actual: int
    n_compared: int
    match_pct: float
    first_divergence: FirstDivergence | None
    divergence_histogram: dict[str, int]
    intent_schema_version: str = INTENT_SCHEMA_VERSION
    hash_version: str = HASH_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["first_divergence"] = self.first_divergence.to_dict() if self.first_divergence else None
        return d


def _stream_schema_version(stream: list[dict[str, Any]]) -> str | None:
    """Return the single declared schema version, or ``"__mixed__"`` if a
    stream disagrees with itself, or ``None`` for an empty stream."""
    versions = {str(r.get("intent_schema_version", INTENT_SCHEMA_VERSION)) for r in stream}
    if not versions:
        return None
    if len(versions) > 1:
        return "__mixed__"
    return next(iter(versions))


def _context(rec: dict[str, Any] | None) -> dict[str, Any]:
    if not rec:
        return {}
    return {k: rec[k] for k in _OPTIONAL_CONTEXT_FIELDS if k in rec}


def _divergence(
    *,
    path_pair: str,
    index: int,
    mismatch_type: MismatchType,
    expected: dict[str, Any] | None,
    actual: dict[str, Any] | None,
    expected_hash: str | None,
    actual_hash: str | None,
) -> FirstDivergence:
    src = expected if expected is not None else (actual or {})
    return FirstDivergence(
        path_pair=path_pair,
        event_index=index,
        mismatch_type=mismatch_type.value,
        symbol=src.get("symbol"),
        source_ts=src.get("source_ts"),
        local_ts=src.get("local_ts"),
        strategy_id=src.get("strategy_id"),
        expected=expected,
        actual=actual,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        context=_context(expected) or _context(actual),
    )


def _field_histogram(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, int]:
    """Full-stream, field-level divergence counts (for the metric / dashboard).

    Length deltas bucket under ``__missing__``; per-position decision-field
    inequalities bucket by field name. This is reporting only — ``ok`` and
    ``first_divergence`` are decided by :func:`diff_intent_streams`.
    """
    hist: dict[str, int] = {}
    n = max(len(expected), len(actual))
    for i in range(n):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e is None or a is None:
            hist["__missing__"] = hist.get("__missing__", 0) + 1
            continue
        for k in HASH_FIELDS:
            if e.get(k) != a.get(k):
                hist[k] = hist.get(k, 0) + 1
    return hist


def diff_intent_streams(
    expected: list[dict[str, Any]] | None,
    actual: list[dict[str, Any]] | None,
    *,
    path_pair: str = "live_vs_replay",
    expect_nonempty: bool = True,
) -> IntentDiffResult:
    """Compare two canonical intent streams and fail closed on any divergence.

    Args:
        expected: live/shadow canonical intents (``None`` = log absent).
        actual: replay-generated canonical intents (``None`` = log absent).
        path_pair: label for the comparison, e.g. ``"live_vs_replay"``.
        expect_nonempty: when True (the default) an empty ``actual`` stream is
            an ``empty_replay`` failure and an empty ``expected`` stream is a
            ``missing_intent_log`` failure — parity cannot be certified against
            no data.

    Returns:
        An :class:`IntentDiffResult` with ``ok=True`` only when both streams
        are non-empty, share a schema version, and are byte-for-byte identical
        on the hashed decision fields in order.
    """
    # 1. Missing intent log (one side was never produced).
    if expected is None or actual is None:
        return IntentDiffResult(
            ok=False,
            path_pair=path_pair,
            n_expected=0 if expected is None else len(expected),
            n_actual=0 if actual is None else len(actual),
            n_compared=0,
            match_pct=0.0,
            first_divergence=_divergence(
                path_pair=path_pair,
                index=0,
                mismatch_type=MismatchType.MISSING_INTENT_LOG,
                expected=None,
                actual=None,
                expected_hash=None,
                actual_hash=None,
            ),
            divergence_histogram={MismatchType.MISSING_INTENT_LOG.value: 1},
        )

    n_exp, n_act = len(expected), len(actual)

    # 2. Schema-version skew (stream-level), checked before content so a
    #    version mismatch is never reported as a field divergence.
    exp_ver = _stream_schema_version(expected)
    act_ver = _stream_schema_version(actual)
    present_vers = {v for v in (exp_ver, act_ver) if v is not None}
    if "__mixed__" in present_vers or len(present_vers) > 1:
        return IntentDiffResult(
            ok=False,
            path_pair=path_pair,
            n_expected=n_exp,
            n_actual=n_act,
            n_compared=0,
            match_pct=0.0,
            first_divergence=_divergence(
                path_pair=path_pair,
                index=0,
                mismatch_type=MismatchType.SCHEMA_MISMATCH,
                expected=expected[0] if expected else None,
                actual=actual[0] if actual else None,
                expected_hash=None,
                actual_hash=None,
            ),
            divergence_histogram={MismatchType.SCHEMA_MISMATCH.value: 1},
        )

    # 2b. Required-decision-field validation. A record that bypassed
    #     canonicalize_intent (e.g. a directly-constructed stream) and is
    #     missing a hashed decision field must fail as schema_mismatch rather
    #     than hash-matching on a defaulted value.
    for stream in (expected, actual):
        for idx, rec in enumerate(stream):
            missing = _first_missing_required(rec)
            if missing is not None:
                on_expected = stream is expected
                return IntentDiffResult(
                    ok=False,
                    path_pair=path_pair,
                    n_expected=n_exp,
                    n_actual=n_act,
                    n_compared=0,
                    match_pct=0.0,
                    first_divergence=_divergence(
                        path_pair=path_pair,
                        index=idx,
                        mismatch_type=MismatchType.SCHEMA_MISMATCH,
                        expected=rec if on_expected else None,
                        actual=None if on_expected else rec,
                        expected_hash=None,
                        actual_hash=None,
                    ),
                    divergence_histogram={MismatchType.SCHEMA_MISMATCH.value: 1},
                )

    # 3. Empty streams — fail closed, never trivially "100%".
    if expect_nonempty and (n_exp == 0 or n_act == 0):
        mt = MismatchType.EMPTY_REPLAY if n_act == 0 else MismatchType.MISSING_INTENT_LOG
        return IntentDiffResult(
            ok=False,
            path_pair=path_pair,
            n_expected=n_exp,
            n_actual=n_act,
            n_compared=max(n_exp, n_act),
            match_pct=0.0,
            first_divergence=_divergence(
                path_pair=path_pair,
                index=0,
                mismatch_type=mt,
                expected=expected[0] if expected else None,
                actual=actual[0] if actual else None,
                expected_hash=None,
                actual_hash=None,
            ),
            divergence_histogram={mt.value: 1},
        )

    exp_hashes = [stable_intent_hash(r) for r in expected]
    act_hashes = [stable_intent_hash(r) for r in actual]
    histogram = _field_histogram(expected, actual)

    # Identical, in order: the only ok=True path.
    if exp_hashes == act_hashes:
        return IntentDiffResult(
            ok=True,
            path_pair=path_pair,
            n_expected=n_exp,
            n_actual=n_act,
            n_compared=max(n_exp, n_act),
            match_pct=100.0,
            first_divergence=None,
            divergence_histogram=histogram,
        )

    # Same multiset, different order → ordering_mismatch.
    same_multiset = Counter(exp_hashes) == Counter(act_hashes)

    first_idx, mismatch_type = _classify_first_divergence(exp_hashes, act_hashes, same_multiset)
    e = expected[first_idx] if first_idx < n_exp else None
    a = actual[first_idx] if first_idx < n_act else None
    n_compared = max(n_exp, n_act)
    n_match = sum(1 for i in range(min(n_exp, n_act)) if exp_hashes[i] == act_hashes[i])
    return IntentDiffResult(
        ok=False,
        path_pair=path_pair,
        n_expected=n_exp,
        n_actual=n_act,
        n_compared=n_compared,
        match_pct=(n_match / n_compared) * 100.0 if n_compared else 0.0,
        first_divergence=_divergence(
            path_pair=path_pair,
            index=first_idx,
            mismatch_type=mismatch_type,
            expected=e,
            actual=a,
            expected_hash=exp_hashes[first_idx] if first_idx < n_exp else None,
            actual_hash=act_hashes[first_idx] if first_idx < n_act else None,
        ),
        divergence_histogram=histogram,
    )


def _classify_first_divergence(
    exp_hashes: list[str],
    act_hashes: list[str],
    same_multiset: bool,
) -> tuple[int, MismatchType]:
    """Locate the first divergence index and classify its mismatch type."""
    n = max(len(exp_hashes), len(act_hashes))
    for i in range(n):
        e = exp_hashes[i] if i < len(exp_hashes) else None
        a = act_hashes[i] if i < len(act_hashes) else None
        if e == a:
            continue
        if a is None:
            return i, MismatchType.MISSING_EXPECTED_INTENT
        if e is None:
            return i, MismatchType.UNEXPECTED_ACTUAL_INTENT
        # Both present but differ.
        if same_multiset:
            return i, MismatchType.ORDERING_MISMATCH
        exp_rest = exp_hashes[i:]
        act_rest = act_hashes[i:]
        # actual inserted an extra intent here (expected[i] reappears later in actual).
        if e in act_rest[1:] and a not in exp_rest:
            return i, MismatchType.UNEXPECTED_ACTUAL_INTENT
        # actual dropped an intent here (actual[i] reappears later in expected).
        if a in exp_rest[1:] and e not in act_rest:
            return i, MismatchType.MISSING_EXPECTED_INTENT
        return i, MismatchType.INTENT_HASH_MISMATCH
    # Hash lists equal in the compared prefix but lengths differ.
    if len(exp_hashes) > len(act_hashes):
        return len(act_hashes), MismatchType.MISSING_EXPECTED_INTENT
    return len(exp_hashes), MismatchType.UNEXPECTED_ACTUAL_INTENT
