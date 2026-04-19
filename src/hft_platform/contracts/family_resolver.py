"""Gate 1 PoC: ContractFamily -> concrete ContractRef resolver.

Single-writer atomic-snapshot resolver that binds family references
(``ContractFamily(TMF:R1)``) to the currently-active concrete
``FutureRef`` derived from an expiry calendar. Broker-agnostic: adapters
(Shioaji, Fubon) supply the raw per-root list of expiries; this module
handles R1/R2 ordering and rollover atomicity.

Design
------
- Snapshot is *immutable*. Rebind happens via ``swap_snapshot`` which
  replaces the whole table and emits ``FamilyBindingChanged`` events for
  hooks. Consumers hold a reference to the snapshot for the duration of a
  tick/event and see a consistent view.
- No background threads or locks — the resolver is synchronously driven by
  the contract-refresh callback path.
- Stateless from the event-loop perspective: no per-tick allocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Mapping

from hft_platform.contracts.ref import (
    ContractFamily,
    ContractRef,
    FamilyCode,
    FutureRef,
    ImmutableContractSnapshot,
    Product,
)

__all__ = [
    "ContractFamilyResolver",
    "FamilyBindingChanged",
    "build_snapshot_from_calendar",
]


@dataclass(frozen=True, slots=True)
class FamilyBindingChanged:
    """Emitted when a family rebinds to a new concrete expiry."""

    family: ContractFamily
    old_ref: ContractRef | None
    new_ref: ContractRef
    snapshot_ns: int


FamilyBindingHook = Callable[[FamilyBindingChanged], None]


class ContractFamilyResolver:
    """Atomic snapshot resolver for ``ContractFamily`` -> ``ContractRef``.

    Consumers call :meth:`resolve_family` per subscription and
    :meth:`swap_snapshot` on rollover / refresh boundaries. Hooks registered
    via :meth:`add_hook` fire with ``FamilyBindingChanged`` whenever a
    family's binding actually changes (hook does **not** fire on no-op
    swaps).
    """

    __slots__ = ("_snapshot", "_hooks")

    def __init__(
        self, *, initial: ImmutableContractSnapshot | None = None
    ) -> None:
        self._snapshot: ImmutableContractSnapshot = (
            initial
            if initial is not None
            else ImmutableContractSnapshot.build(
                family_map={}, native_hints={}, snapshot_ns=0
            )
        )
        self._hooks: list[FamilyBindingHook] = []

    @property
    def snapshot(self) -> ImmutableContractSnapshot:
        """Current immutable snapshot. Safe to hold across a tick."""
        return self._snapshot

    def add_hook(self, hook: FamilyBindingHook) -> None:
        """Register a callback fired for every ``FamilyBindingChanged``."""
        self._hooks.append(hook)

    def resolve_family(self, family: ContractFamily) -> ContractRef | None:
        return self._snapshot.resolve_family(family)

    def swap_snapshot(
        self, new_snapshot: ImmutableContractSnapshot
    ) -> list[FamilyBindingChanged]:
        """Replace the binding table atomically and fire hooks for diffs."""
        old = self._snapshot
        self._snapshot = new_snapshot
        changes: list[FamilyBindingChanged] = []
        seen: set[ContractFamily] = set()
        for family, new_ref in new_snapshot.family_map.items():
            seen.add(family)
            old_ref = old.family_map.get(family)
            if old_ref != new_ref:
                changes.append(
                    FamilyBindingChanged(
                        family=family,
                        old_ref=old_ref,
                        new_ref=new_ref,
                        snapshot_ns=new_snapshot.snapshot_ns,
                    )
                )
        # Removals: families present in old but not in new (e.g., last expiry
        # delisted). We do not emit a change for these here because no new
        # binding is available; callers should poll snapshot for absence.
        for hook in self._hooks:
            for change in changes:
                try:
                    hook(change)
                except Exception:  # noqa: BLE001 — hooks must not crash resolver
                    pass
        return changes


def build_snapshot_from_calendar(
    calendars: Mapping[str, list[FutureRef]],
    *,
    today: date,
    snapshot_ns: int,
    native_hints: Mapping[str, object] | None = None,
) -> ImmutableContractSnapshot:
    """Construct an ``ImmutableContractSnapshot`` from per-root expiry lists.

    Each root's non-expired ``FutureRef`` list is sorted ascending by
    expiry; the first two become R1 and R2. Refs with ``expiry < today``
    are excluded. Additional expiries (R3+) are not currently bound —
    extend ``_FAMILY_ORDER`` to enable.
    """
    family_map: dict[ContractFamily, ContractRef] = {}
    for root, refs in calendars.items():
        active = sorted(
            (r for r in refs if r.expiry >= today), key=lambda r: r.expiry
        )
        for position, ref in enumerate(active[: len(_FAMILY_ORDER)]):
            family_code = _FAMILY_ORDER[position]
            family = ContractFamily(
                product=Product.FUTURE, root=root, family=family_code
            )
            bound = FutureRef(
                root=ref.root, expiry=ref.expiry, family=family_code
            )
            family_map[family] = bound
    return ImmutableContractSnapshot.build(
        family_map=family_map,
        native_hints=dict(native_hints or {}),
        snapshot_ns=snapshot_ns,
    )


_FAMILY_ORDER: tuple[FamilyCode, ...] = (FamilyCode.R1, FamilyCode.R2)
