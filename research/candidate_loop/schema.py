"""Candidate JSON schema, enums, and content-addressed IDs (spec §6/§13).

The candidate contract is frozen as ``schema_version = cand_v1`` /
``primitive_version = prim_v1``.  ``alpha_id`` is content-addressed over the
canonical JSON of the whole candidate so re-ingesting the same JSONL line is
idempotent.  ``formula_hash`` (dedupe key) lives in ``validator.py`` because it
requires the parsed, feature-inlined signal AST.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from typing import Any

import msgspec

SCHEMA_VERSION = "cand_v1"
PRIMITIVE_VERSION = "prim_v1"

FAMILIES = frozenset(
    {
        "order_book_imbalance",
        "microprice",
        "depth_delta",
        "trade_flow",
        "spread_regime",
        "replenishment",
    }
)

CANDIDATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,64}$")
FEATURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")

HYPOTHESIS_MIN_CHARS = 20
HYPOTHESIS_MAX_CHARS = 500
MAX_FEATURES = 6

# prim_v1 whitelists (spec §7). Signatures map name -> ordered parameter
# names; the first transform parameter is always the input expression.
PRIMITIVE_SIGNATURES: dict[str, tuple[str, ...]] = {
    "mid_price": (),
    "spread_ticks": (),
    "depth_sum": ("side", "levels"),
    "book_imbalance": ("levels",),
    "microprice": (),
    "depth_delta": ("side", "levels", "window"),
    "trade_imbalance": ("window",),
    "future_mid_return": ("horizon",),  # LABEL ONLY
}
TRANSFORM_SIGNATURES: dict[str, tuple[str, ...]] = {
    "zscore": ("x", "window"),
    "negative_zscore": ("x", "window"),
    "ema": ("x", "window"),
    "clip": ("x", "lo", "hi"),
}
TRANSFORM_DEFAULTS: dict[str, dict[str, str]] = {
    "zscore": {"window": "2000_events"},
    "negative_zscore": {"window": "2000_events"},
}
LABEL_PRIMITIVE = "future_mid_return"
SIDES = frozenset({"bid", "ask"})

# Window/horizon domains (spec §7).
EVENT_WINDOW_MIN, EVENT_WINDOW_MAX = 10, 10_000
TIME_WINDOW_MIN_NS, TIME_WINDOW_MAX_NS = 50_000_000, 60_000_000_000  # 50ms..60s
HORIZON_TIME_MIN_NS, HORIZON_TIME_MAX_NS = 100_000_000, 30_000_000_000  # 100ms..30s
HORIZON_EVENT_MIN, HORIZON_EVENT_MAX = 1, 10_000

_WINDOW_RE = re.compile(r"^(\d+)(_events|ms|s)$")


class Status(str, enum.Enum):
    NEW = "NEW"
    INVALID = "INVALID"
    COMPILED = "COMPILED"
    EVALUATED = "EVALUATED"
    REJECTED = "REJECTED"
    WATCHLIST = "WATCHLIST"
    PROMOTED = "PROMOTED"


class DeathReason(str, enum.Enum):
    # validator
    SCHEMA_INVALID = "SCHEMA_INVALID"
    FORMULA_PARSE_ERROR = "FORMULA_PARSE_ERROR"
    PRIMITIVE_INVALID = "PRIMITIVE_INVALID"
    UNSUPPORTED_NEW_PRIMITIVE = "UNSUPPORTED_NEW_PRIMITIVE"
    ARGUMENT_INVALID = "ARGUMENT_INVALID"
    OVER_COMPLEX = "OVER_COMPLEX"
    DUPLICATE_ALPHA = "DUPLICATE_ALPHA"
    # evaluator
    NO_SIGNAL = "NO_SIGNAL"
    SIGN_UNSTABLE = "SIGN_UNSTABLE"
    COST_KILLED = "COST_KILLED"
    LATENCY_KILLED = "LATENCY_KILLED"
    ONE_DAY_ONLY = "ONE_DAY_ONLY"
    REGIME_ONLY = "REGIME_ONLY"


class ProposedPrimitive(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    reason: str
    required_data: list[str] = []
    not_executable_in_v1: bool = True  # must be true; validator rejects false


class Feature(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str  # FEATURE_NAME_RE
    formula: str


class Candidate(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str  # CANDIDATE_NAME_RE
    family: str  # in FAMILIES
    hypothesis: str  # 20-500 chars
    features: list[Feature]  # 1-6 entries
    signal_formula: str  # over feature names + primitives + transforms
    label: str  # exactly future_mid_return(horizon=...)
    horizon: str  # 'Nms' | 'Ns' | 'N_events'
    expected_sign: str  # 'positive' | 'negative'
    regime_filter: str = ""  # '' = always-on; else one comparison expr
    cost_risk: str = ""
    latency_risk: str = ""
    falsification_tests: list[str] = []
    proposed_new_primitives: list[ProposedPrimitive] = []  # recorded, NEVER executed


class Window(msgspec.Struct, frozen=True):
    """Parsed window/horizon: kind 'events' (count) or 'time' (nanoseconds)."""

    kind: str  # 'events' | 'time'
    count: int = 0  # events
    duration_ns: int = 0  # time


def parse_window_spec(spec: str) -> Window:
    """Parse ``'N_events' | 'Nms' | 'Ns'`` into a :class:`Window`.

    Format-only; range checks belong to the validator (windows and horizons
    have different domains).  Raises ``ValueError`` on malformed input.
    """
    m = _WINDOW_RE.match(spec)
    if m is None:
        raise ValueError(f"Malformed window/horizon spec {spec!r} (want 'N_events'|'Nms'|'Ns')")
    n = int(m.group(1))
    unit = m.group(2)
    if n <= 0:
        raise ValueError(f"Window/horizon must be positive: {spec!r}")
    if unit == "_events":
        return Window(kind="events", count=n)
    ns = n * (1_000_000 if unit == "ms" else 1_000_000_000)
    return Window(kind="time", duration_ns=ns)


def canonical_json(candidate: Candidate) -> str:
    """Deterministic JSON: builtins, sorted keys, compact separators."""
    payload = msgspec.to_builtins(candidate)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_alpha_id(candidate: Candidate) -> str:
    """``sha256(canonical_json(candidate))[:16]`` (spec §6)."""
    return hashlib.sha256(canonical_json(candidate).encode("utf-8")).hexdigest()[:16]


def candidate_json_schema() -> dict[str, Any]:
    """JSON Schema for the candidate contract, generated from the structs.

    Shipped with every generation prompt so cheap models target the exact
    contract (spec §6).
    """
    return msgspec.json.schema(Candidate)


__all__ = [
    "CANDIDATE_NAME_RE",
    "Candidate",
    "DeathReason",
    "EVENT_WINDOW_MAX",
    "EVENT_WINDOW_MIN",
    "FAMILIES",
    "FEATURE_NAME_RE",
    "Feature",
    "HORIZON_EVENT_MAX",
    "HORIZON_EVENT_MIN",
    "HORIZON_TIME_MAX_NS",
    "HORIZON_TIME_MIN_NS",
    "HYPOTHESIS_MAX_CHARS",
    "HYPOTHESIS_MIN_CHARS",
    "LABEL_PRIMITIVE",
    "MAX_FEATURES",
    "PRIMITIVE_SIGNATURES",
    "PRIMITIVE_VERSION",
    "ProposedPrimitive",
    "SCHEMA_VERSION",
    "SIDES",
    "Status",
    "TIME_WINDOW_MAX_NS",
    "TIME_WINDOW_MIN_NS",
    "TRANSFORM_DEFAULTS",
    "TRANSFORM_SIGNATURES",
    "Window",
    "candidate_json_schema",
    "canonical_json",
    "compute_alpha_id",
    "parse_window_spec",
]
