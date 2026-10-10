"""Remove duplicate deliveries of the same market event from raw ClickHouse rows.

Why this is not "drop identical rows": the recorder sometimes delivered every event several
times. Copies share one ``seq_no`` sequence, so they come from one process, and the number of
copies (the *multiplicity*) changes by period, symbol and hour: 2x on 2026-01-27 -> 02-26,
about 4x on 04-28 -> 06-01, clean from 06-03. Two genuinely distinct trades can also share
``(exch_ts, price, volume)``, so collapsing every identical key to one row (the round-1 rule)
under-counts real volume by about 3%.

Rule (``RULE_ID``): for each content key ``(exch_ts, price_scaled, volume)`` count its copies
``c``. Within a time bucket the *multiplicity* ``m`` is the most common ``c`` over the keys in
that bucket (ties go to the smaller value, capped at ``MAX_MULTIPLICITY``). Each key then
represents ``ceil(c / m)`` events and the first that many rows are kept. A real event delivered
``m`` times has ``c == m``; two real events at one key have ``c == 2m`` and keep two rows.

A bucket with fewer than ``MIN_KEYS`` distinct keys is too thin to estimate from, so it uses the
enclosing hour, then the enclosing day, and finally ``m = 1`` (nothing is removed).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from math import ceil
from typing import Any

RULE_ID = "content_multiplicity_v1"
BUCKET_SECONDS = 300
MIN_KEYS = 20
MAX_MULTIPLICITY = 8

_NS = 1_000_000_000
_HOUR_NS = 3600 * _NS
_DAY_NS = 24 * 3600 * _NS


def estimate_multiplicity(copy_counts: Sequence[int], *, max_multiplicity: int = MAX_MULTIPLICITY) -> int:
    """Most common copy count (ties -> smaller), clipped to ``[1, max_multiplicity]``."""
    if not copy_counts:
        return 1
    tally = Counter(copy_counts)
    top = max(tally.values())
    mode = min(count for count, seen in tally.items() if seen == top)
    return max(1, min(int(mode), max_multiplicity))


@dataclass(slots=True)
class DedupStats:
    """What a dedup pass did. ``multiplicity_keys`` maps m -> number of distinct keys decided at m."""

    rule: str = RULE_ID
    bucket_seconds: int = BUCKET_SECONDS
    rows_in: int = 0
    rows_out: int = 0
    keys: int = 0
    keys_by_level: dict[str, int] = field(default_factory=dict)
    multiplicity_keys: dict[int, int] = field(default_factory=dict)

    @property
    def removed(self) -> int:
        return self.rows_in - self.rows_out

    def to_payload(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "bucket_seconds": self.bucket_seconds,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "removed": self.removed,
            "keys": self.keys,
            "keys_by_level": dict(self.keys_by_level),
            "multiplicity_keys": {str(m): n for m, n in sorted(self.multiplicity_keys.items())},
        }


def keep_counts(
    keys: Sequence[tuple[int, Hashable, Hashable]],
    *,
    bucket_seconds: int = BUCKET_SECONDS,
    min_keys: int = MIN_KEYS,
    max_multiplicity: int = MAX_MULTIPLICITY,
) -> tuple[dict[tuple[int, Hashable, Hashable], int], DedupStats]:
    """For every distinct ``(exch_ts, a, b)`` key, how many of its rows to keep.

    ``exch_ts`` is nanoseconds since the epoch; buckets are cut on UTC boundaries, which is
    only a grouping device (any fixed grid works).
    """
    bucket_ns = bucket_seconds * _NS
    copies = Counter(keys)
    stats = DedupStats(bucket_seconds=bucket_seconds, rows_in=len(keys), keys=len(copies))

    levels: tuple[tuple[str, int], ...] = (("bucket", bucket_ns), ("hour", _HOUR_NS), ("day", _DAY_NS))
    by_level: list[dict[int, list[int]]] = []
    for _, width in levels:
        grouped: dict[int, list[int]] = {}
        for key, count in copies.items():
            grouped.setdefault(key[0] // width, []).append(count)
        by_level.append(grouped)
    multiplicity_cache: dict[tuple[int, int], int | None] = {}

    def decide(level: int, group: int) -> int | None:
        cached = multiplicity_cache.get((level, group), -1)
        if cached != -1:
            return cached
        counts = by_level[level][group]
        value = estimate_multiplicity(counts, max_multiplicity=max_multiplicity) if len(counts) >= min_keys else None
        multiplicity_cache[(level, group)] = value
        return value

    keep: dict[tuple[int, Hashable, Hashable], int] = {}
    for key, count in copies.items():
        multiplicity = 1
        used = "none"
        for level, (name, width) in enumerate(levels):
            decided = decide(level, key[0] // width)
            if decided is not None:
                multiplicity, used = decided, name
                break
        keep[key] = ceil(count / multiplicity)
        stats.keys_by_level[used] = stats.keys_by_level.get(used, 0) + 1
        stats.multiplicity_keys[multiplicity] = stats.multiplicity_keys.get(multiplicity, 0) + 1
    stats.rows_out = sum(keep.values())
    return keep, stats


def dedup_tick_rows(
    rows: Sequence[Sequence[Any]],
    *,
    bucket_seconds: int = BUCKET_SECONDS,
) -> tuple[list[Sequence[Any]], DedupStats]:
    """Dedup the ``Tick`` rows of one symbol-day, leaving every other row where it was.

    ``rows`` are the exporter's ordered rows ``(type, exch_ts, local_ts, bids_price, asks_price,
    bids_vol, asks_vol, price_scaled, volume)``. Order is preserved and the *first* ``keep``
    rows of each key survive.
    """
    keys: list[tuple[int, int, int]] = []
    for row in rows:
        if row[0] == "Tick":
            keys.append((int(row[1]), int(row[7] or 0), int(row[8] or 0)))
    keep, stats = keep_counts(keys, bucket_seconds=bucket_seconds)
    remaining = dict(keep)
    out: list[Sequence[Any]] = []
    for row in rows:
        if row[0] != "Tick":
            out.append(row)
            continue
        key = (int(row[1]), int(row[7] or 0), int(row[8] or 0))
        if remaining[key] > 0:
            remaining[key] -= 1
            out.append(row)
    return out, stats
