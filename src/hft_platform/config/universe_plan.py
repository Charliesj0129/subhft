"""Plan a subscription-universe roll: validate a rebuilt universe, then place
its codes on quote connections without disturbing the ones already subscribed.

A monthly contract settles and the broker delists it. ``symbols.list`` is
written in relative terms (``TXF@front``, ``OPT@TXO@near@ATM+/-26``), so
rebuilding it against a fresh contract index already yields the new month --
see ``config.symbols.build_symbols``. What was missing is everything after the
rebuild: deciding whether the result is safe to adopt, and which connection
each new code belongs on.

Both steps are pure functions here so they can be tested without a broker, a
pool, or a clock:

- :func:`validate_universe` refuses a universe that is empty, holds a settled
  contract, repeats a code, or cannot fit on the connections available. A
  refused roll leaves the running universe alone, which is the fail-closed
  direction: subscriptions that work today keep working.
- :func:`plan_shards` keeps every surviving code on the connection it is
  already subscribed on, so a roll never re-subscribes an unrelated live
  contract (R47's TMF front month is the one that matters). Only settled codes
  are unsubscribed, and only genuinely new codes are placed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = [
    "ShardPlan",
    "UniversePlanError",
    "plan_shards",
    "validate_universe",
]


class UniversePlanError(Exception):
    """Raised when a rebuilt universe cannot be placed on the connections."""


@dataclass(frozen=True)
class ShardPlan:
    """Where every code of the new universe goes, and what changes.

    ``groups`` is the full per-connection placement (what each shard file and
    facade config becomes). ``added`` and ``removed`` are the deltas the caller
    must actually apply to the broker: subscribe the former, unsubscribe the
    latter. Codes in neither are already subscribed on the right connection and
    must be left untouched.
    """

    groups: dict[int, list[dict[str, Any]]]
    added: dict[int, list[str]] = field(default_factory=dict)
    removed: dict[int, list[str]] = field(default_factory=dict)

    @property
    def added_codes(self) -> set[str]:
        return {code for codes in self.added.values() for code in codes}

    @property
    def removed_codes(self) -> set[str]:
        return {code for codes in self.removed.values() for code in codes}

    @property
    def changed(self) -> bool:
        return bool(self.added_codes or self.removed_codes)


def validate_universe(
    symbols: Sequence[Mapping[str, Any]],
    *,
    delivery_of: Callable[[str], date | None],
    cutoff: date,
    num_conns: int,
    per_conn_cap: int,
    min_symbols: int = 1,
) -> list[str]:
    """Return the reasons ``symbols`` must not be adopted; empty means safe.

    ``delivery_of`` maps a code to its delivery date (``None`` when the broker
    has no date for it, e.g. equities), and ``cutoff`` is the exchange date a
    contract must still be deliverable on -- see
    ``core.market_calendar.taifex_delivery_cutoff``.
    """
    errors: list[str] = []

    codes: list[str] = []
    for entry in symbols:
        code = str(entry.get("code") or "").strip()
        if not code:
            errors.append("symbol entry without a code")
            continue
        codes.append(code)

    if len(codes) < min_symbols:
        errors.append(f"universe holds {len(codes)} symbols, needs at least {min_symbols}")

    duplicates = sorted(code for code, seen in Counter(codes).items() if seen > 1)
    if duplicates:
        errors.append(f"duplicate codes: {', '.join(duplicates[:5])}")

    capacity = max(0, num_conns) * max(0, per_conn_cap)
    if len(codes) > capacity:
        errors.append(f"universe of {len(codes)} exceeds pool capacity {capacity} ({num_conns}x{per_conn_cap})")

    settled: list[str] = []
    for code in codes:
        delivery = delivery_of(code)
        if delivery is not None and delivery < cutoff:
            settled.append(f"{code}@{delivery.isoformat()}")
    if settled:
        errors.append(f"settled contracts in rebuilt universe (cutoff {cutoff.isoformat()}): {', '.join(settled[:5])}")

    return errors


def _index_current_placement(
    current_groups: Mapping[int, Sequence[Mapping[str, Any]]],
    num_conns: int,
) -> tuple[dict[str, int], dict[int, list[str]]]:
    """Return (code -> connection) and (connection -> codes) for what is placed now.

    A connection index that no longer exists is dropped: its codes count as
    unplaced, and whatever is still subscribed there belongs to the caller's
    reconnect path, not to a placement plan.
    """
    placed_at: dict[str, int] = {}
    current_codes: dict[int, list[str]] = {}
    for group_id, entries in current_groups.items():
        if not isinstance(group_id, int) or group_id < 0 or group_id >= num_conns:
            continue
        codes: list[str] = []
        for entry in entries:
            code = str(entry.get("code") or "").strip()
            if not code:
                continue
            codes.append(code)
            placed_at.setdefault(code, group_id)
        current_codes[group_id] = codes
    return placed_at, current_codes


def plan_shards(
    current_groups: Mapping[int, Sequence[Mapping[str, Any]]],
    new_symbols: Sequence[Mapping[str, Any]],
    *,
    num_conns: int,
    per_conn_cap: int,
) -> ShardPlan:
    """Place ``new_symbols`` across ``num_conns`` connections, minimising churn.

    A code already placed on a valid connection keeps it. New codes go to the
    least-loaded connection with room, in sorted order so the same inputs always
    produce the same placement. Raises :class:`UniversePlanError` if the
    placement cannot fit -- the caller keeps the running universe.
    """
    if num_conns <= 0:
        raise UniversePlanError(f"num_conns must be positive, got {num_conns}")

    placed_at, current_codes = _index_current_placement(current_groups, num_conns)

    groups: dict[int, list[dict[str, Any]]] = {group_id: [] for group_id in range(num_conns)}
    added: dict[int, list[str]] = {group_id: [] for group_id in range(num_conns)}
    removed: dict[int, list[str]] = {group_id: [] for group_id in range(num_conns)}

    unplaced: list[dict[str, Any]] = []
    new_codes: set[str] = set()
    for entry in new_symbols:
        code = str(entry.get("code") or "").strip()
        if not code:
            continue
        new_codes.add(code)
        placement = placed_at.get(code)
        if placement is None:
            unplaced.append(dict(entry))
        else:
            kept_entry = dict(entry)
            kept_entry["group"] = placement
            groups[placement].append(kept_entry)

    for group_id, codes in current_codes.items():
        removed[group_id] = [code for code in codes if code not in new_codes]

    unplaced.sort(key=lambda entry: str(entry.get("code") or ""))
    for entry in unplaced:
        target = min(range(num_conns), key=lambda group_id: (len(groups[group_id]), group_id))
        if len(groups[target]) >= per_conn_cap:
            raise UniversePlanError(
                f"cannot place {entry.get('code')!r}: every connection is at the "
                f"{per_conn_cap}-subscription cap ({num_conns} connections)"
            )
        entry["group"] = target
        groups[target].append(entry)
        added[target].append(str(entry.get("code") or ""))

    for group_id, entries in groups.items():
        # Reachable without any new code: a connection can already hold more
        # than the cap if the cap was lowered, or if a shard was written by an
        # older build. Refuse rather than carry the overflow into a roll.
        if len(entries) > per_conn_cap:
            raise UniversePlanError(
                f"connection {group_id} would hold {len(entries)} subscriptions, cap is {per_conn_cap}"
            )

    return ShardPlan(groups=groups, added=added, removed=removed)
