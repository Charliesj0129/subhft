"""Divergence-category classifier (goal §8).

The replay-parity harness records per-field divergence counts as
``ReplayParityReport.divergence_histogram: dict[str, int]``.  Goal §8
requires every observed inconsistency be classified into one of nine
named categories so triage can route the failure to the right owner:

    data_mismatch
    feature_mismatch
    timestamp_alignment_error
    latency_shift
    session_phase_filter
    risk_filter
    position_limit
    implementation_drift
    unknown

This module maps the canonical intent-log field names (see
``src/hft_platform/replay/intent_log.py::_intent_to_canonical``) to
categories.  Unknown keys land in ``UNKNOWN`` rather than being
silently dropped — masking divergences is worse than mislabelling
them, since triage can still see the count.

Extending the mapping: add the field to ``_FIELD_TO_CATEGORY``.  Adding
a new canonical intent field without also updating this mapping will
cause ``test_known_intent_fields_have_non_unknown_mapping`` to fail.
"""

from __future__ import annotations

from collections import defaultdict
from enum import Enum


class DivergenceCategory(str, Enum):
    DATA_MISMATCH = "data_mismatch"
    FEATURE_MISMATCH = "feature_mismatch"
    TIMESTAMP_ALIGNMENT_ERROR = "timestamp_alignment_error"
    LATENCY_SHIFT = "latency_shift"
    SESSION_PHASE_FILTER = "session_phase_filter"
    RISK_FILTER = "risk_filter"
    POSITION_LIMIT = "position_limit"
    IMPLEMENTATION_DRIFT = "implementation_drift"
    UNKNOWN = "unknown"


# Canonical intent-log fields → category.
# Rationale (per goal §8 semantics):
# - __missing__       : length mismatch ⇒ live and replay disagree on
#                       the data record they observed.
# - symbol            : same.
# - timestamp_us      : ts alignment by definition.
# - decision_price    : the price at decision time drifts when the
#                       feature snapshot read at a different latency.
# - qty               : per CLAUDE.md, sizing is governed by
#                       position-sizing rule + risk caps; qty drift in
#                       intents is most often a position_limit divergence.
# - strategy_id /
#   intent_id /
#   intent_type /
#   side / tif /
#   price /
#   target_order_id /
#   price_type        : same code, same inputs ⇒ identifier or
#                       behavioural divergence is implementation drift.
_FIELD_TO_CATEGORY: dict[str, DivergenceCategory] = {
    "__missing__": DivergenceCategory.DATA_MISMATCH,
    "symbol": DivergenceCategory.DATA_MISMATCH,
    "timestamp_us": DivergenceCategory.TIMESTAMP_ALIGNMENT_ERROR,
    "decision_price": DivergenceCategory.LATENCY_SHIFT,
    "qty": DivergenceCategory.POSITION_LIMIT,
    "strategy_id": DivergenceCategory.IMPLEMENTATION_DRIFT,
    "intent_id": DivergenceCategory.IMPLEMENTATION_DRIFT,
    "intent_type": DivergenceCategory.IMPLEMENTATION_DRIFT,
    "side": DivergenceCategory.IMPLEMENTATION_DRIFT,
    "tif": DivergenceCategory.IMPLEMENTATION_DRIFT,
    "price": DivergenceCategory.IMPLEMENTATION_DRIFT,
    "target_order_id": DivergenceCategory.IMPLEMENTATION_DRIFT,
    "price_type": DivergenceCategory.IMPLEMENTATION_DRIFT,
}


def classify_field(field_name: str) -> DivergenceCategory:
    """Return the category for a histogram field, falling back to UNKNOWN."""
    return _FIELD_TO_CATEGORY.get(field_name, DivergenceCategory.UNKNOWN)


def categorize_histogram(histogram: dict[str, int]) -> dict[str, int]:
    """Aggregate a per-field divergence histogram into category counts.

    Returns a dict keyed by category string value (so it round-trips
    through JSON without enum-class knowledge).  Only categories with
    non-zero counts are returned — keeps the metrics dict compact for
    downstream logs.
    """
    counts: dict[str, int] = defaultdict(int)
    for field_name, count in histogram.items():
        if not count:
            continue
        category = classify_field(field_name)
        counts[category.value] += int(count)
    return dict(counts)
