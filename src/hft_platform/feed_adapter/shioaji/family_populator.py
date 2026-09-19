"""Populate a :class:`ContractFamilyResolver` from a Shioaji contract table.

Reads ``api.Contracts.Futures.<root>`` and extracts every concrete expiry's
``delivery_month`` / ``delivery_date``, building a per-root list of
:class:`FutureRef`. The resolver's snapshot is then swapped atomically so
every consumer sees a consistent binding.

Design
------
* Runs **after** broker login fills the contract table (post-connect hook).
* Re-evaluates that table when a delivery date passes
  (:meth:`ShioajiFamilyPopulator.roll_if_due`), because a contract settling
  is not a connect and would otherwise leave strategies on it.
* Ignores R1/R2/C0/C1 alias entries — they carry no expiry of their own;
  the family binding itself is what this module builds.
* Failures in per-contract parsing are logged once and do not abort the
  whole refresh — one bad contract must not poison the binding table.
"""

from __future__ import annotations

import asyncio
from calendar import monthrange
from datetime import date
from typing import Any

from structlog import get_logger

from hft_platform.contracts.family_resolver import (
    ContractFamilyResolver,
    build_snapshot_from_calendar,
)
from hft_platform.contracts.ref import FutureRef
from hft_platform.core import timebase
from hft_platform.core.market_calendar import taifex_delivery_cutoff
from hft_platform.feed_adapter.shioaji._compat import contract_category_groups

logger = get_logger("feed_adapter.shioaji.family_populator")

_ALIAS_SUFFIXES: frozenset[str] = frozenset({"R1", "R2", "C0", "C1"})


def _parse_delivery_to_date(value: Any) -> date | None:
    """Parse Shioaji's ``delivery_month`` / ``delivery_date`` to a :class:`date`.

    Accepts ``"YYYY/MM"``, ``"YYYYMM"``, ``"YYYY/MM/DD"``, ``"YYYYMMDD"``.
    When only year+month are available the last day of that month is used
    as a conservative expiry placeholder (real expiry day is exchange
    business-calendar specific; close enough for "is this expired" checks).
    """
    if value is None:
        return None
    raw = str(value).replace("/", "").replace("-", "")
    try:
        if len(raw) >= 8:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        if len(raw) >= 6:
            year, month = int(raw[:4]), int(raw[4:6])
            last_day = monthrange(year, month)[1]
            return date(year, month, last_day)
    except (ValueError, TypeError):
        return None
    return None


def _extract_futures(
    api: Any,
) -> tuple[dict[str, list[FutureRef]], dict[str, object]]:
    """Walk ``api.Contracts.Futures`` and build:

    1. per-root :class:`FutureRef` calendars for the resolver, and
    2. a ``native_hints`` map keyed by canonical display string
       (``FutureRef.display()``) and valued by the live broker
       ``Contract`` object so :class:`OrderAdapter` can bypass its own
       lookup path.
    """
    if api is None:
        # Silence here cost two months of dead bindings: the pool object the
        # platform passes as ``md_client`` had no ``.api``, so this returned an
        # empty calendar with no trace at all. An unreachable contract table is
        # a failure, not a quiet no-op.
        logger.warning(
            "shioaji_family_populator_no_api",
            note="broker client exposed no .api handle; family bindings not refreshed",
        )
        return {}, {}
    contracts = getattr(api, "Contracts", None)
    futures = getattr(contracts, "Futures", None) if contracts is not None else None
    if futures is None:
        logger.warning(
            "shioaji_family_populator_no_contracts",
            has_contracts=contracts is not None,
            note="api.Contracts.Futures unavailable; family bindings not refreshed",
        )
        return {}, {}
    try:
        groups = contract_category_groups(futures)
    except Exception as exc:  # noqa: BLE001
        logger.debug("shioaji_family_populator_roots_failed", error=str(exc))
        return {}, {}

    out: dict[str, list[FutureRef]] = {}
    native_hints: dict[str, object] = {}
    for root, contract_list in groups.items():
        refs: list[FutureRef] = []
        for contract in contract_list:
            code = str(getattr(contract, "code", "") or "")
            suffix = code[-2:] if len(code) >= 4 else ""
            if suffix in _ALIAS_SUFFIXES:
                continue
            expiry = _parse_delivery_to_date(
                getattr(contract, "delivery_date", None) or getattr(contract, "delivery_month", None)
            )
            if expiry is None:
                continue
            ref = FutureRef(root=str(root), expiry=expiry)
            refs.append(ref)
            # native_hint key is the canonical display form. A later
            # ``FutureRef`` rebound to R1 keeps the same display string,
            # so OrderAdapter can look the Contract up via ``ref.display()``
            # without caring about family code.
            native_hints[ref.display()] = contract
            # Also map the broker-side code directly (may equal display
            # for Shioaji, but keeps the door open for codes that diverge
            # from our canonical form).
            if code:
                native_hints.setdefault(code, contract)
        if refs:
            out[str(root)] = refs
    return out, native_hints


def _install_snapshot(
    resolver: ContractFamilyResolver,
    calendars: dict[str, list[FutureRef]],
    native_hints: dict[str, object],
    *,
    cutoff: date,
    snapshot_ns: int,
) -> int:
    snapshot = build_snapshot_from_calendar(
        calendars,
        today=cutoff,
        snapshot_ns=snapshot_ns,
        native_hints=native_hints,
    )
    resolver.swap_snapshot(snapshot)
    bindings = len(snapshot.family_map)
    if bindings:
        logger.info(
            "shioaji_family_populator_snapshot_installed",
            roots=sorted(calendars.keys()),
            bindings=bindings,
            native_hints=len(native_hints),
            delivery_cutoff=cutoff.isoformat(),
        )
    else:
        # Zero bindings means every strategy keeps whatever symbols its config
        # named — including expiries that rolled off months ago. That is the
        # failure this module exists to prevent, so it must not log as routine.
        logger.warning(
            "shioaji_family_populator_no_bindings",
            roots=sorted(calendars.keys()),
            native_hints=len(native_hints),
            note="strategy.symbols will not be rebound to the front month",
        )
    return bindings


def populate_resolver_from_shioaji(
    resolver: ContractFamilyResolver,
    api: Any,
    *,
    today: date | None = None,
) -> int:
    """Swap ``resolver``'s snapshot to one derived from the Shioaji contract
    table. Returns the number of family bindings installed. Idempotent —
    repeat calls with an unchanged contract table produce no rebinds (and
    therefore no hook fires).

    ``today`` is the earliest delivery date that may still be bound. Left out,
    it is :func:`taifex_delivery_cutoff` of now: exchange-local, and already
    tomorrow once a delivery day's final session has closed.
    """
    calendars, native_hints = _extract_futures(api)
    snapshot_ns = timebase.now_ns()
    cutoff = today if today is not None else taifex_delivery_cutoff(snapshot_ns)
    return _install_snapshot(resolver, calendars, native_hints, cutoff=cutoff, snapshot_ns=snapshot_ns)


class ShioajiFamilyPopulator:
    """Keeps a resolver on the front month between connects, not only at them.

    :meth:`populate` reads the broker contract table and runs from the
    post-connect hook chain, as :func:`populate_resolver_from_shioaji` always
    has. That was the *only* trigger, so a binding was decided once per
    connect. THESHOW connected on 2026-09-15 with TMFI6 as the front month,
    TMFI6 settled at 13:30 the next day, and nothing asked again: R47 stayed
    bound to a dead contract and did not trade for days while TMFJ6 quoted
    normally.

    A roll needs no new broker data. The table read at connect already holds
    the next month, since R1 and R2 are both listed before R1 settles. What
    changes is the clock. So :meth:`roll_if_due` re-evaluates the table from the
    last read against the current delivery cutoff, without touching the SDK.
    No SDK access means no ``Already borrowed`` and no broker-thread handoff.
    It runs on the event loop, where the rebind hooks mutate
    ``strategy.symbols``.
    """

    __slots__ = ("_resolver", "_table", "_expiries", "_cutoff")

    def __init__(self, resolver: ContractFamilyResolver) -> None:
        self._resolver = resolver
        # One tuple, assigned whole, so a reader never sees one connect's
        # calendars paired with another connect's native hints.
        self._table: tuple[dict[str, list[FutureRef]], dict[str, object]] = ({}, {})
        self._expiries: frozenset[date] = frozenset()
        self._cutoff: date | None = None

    @property
    def delivery_cutoff(self) -> date | None:
        """The cutoff the installed snapshot was built against, or None before the first read."""
        return self._cutoff

    def populate(self, api: Any, *, now_ns: int | None = None) -> int:
        """Read the contract table and install a snapshot. Returns the binding count."""
        calendars, native_hints = _extract_futures(api)
        snapshot_ns = timebase.now_ns() if now_ns is None else now_ns
        cutoff = taifex_delivery_cutoff(snapshot_ns)
        self._table = (calendars, native_hints)
        self._expiries = frozenset(ref.expiry for refs in calendars.values() for ref in refs)
        self._cutoff = cutoff
        return _install_snapshot(self._resolver, calendars, native_hints, cutoff=cutoff, snapshot_ns=snapshot_ns)

    def roll_if_due(self, *, now_ns: int | None = None) -> int:
        """Rebind every family whose bound contract has settled since the last install.

        Returns the number of families rebound. Cheap when nothing is due: a
        date comparison, and a scan of the handful of distinct delivery dates
        once a day when the cutoff advances. The rebuild itself was measured at
        about 1.1 ms plus 0.7 ms for the swap, for 330 roots with seven
        expiries each (the production contract table has about 390 futures
        roots). It runs once a month, on a delivery day at 13:30.
        """
        prev = self._cutoff
        if prev is None:
            return 0  # nothing read yet; the first connect installs the table
        snapshot_ns = timebase.now_ns() if now_ns is None else now_ns
        cutoff = taifex_delivery_cutoff(snapshot_ns)
        if cutoff <= prev:
            return 0
        self._cutoff = cutoff
        if not any(prev <= expiry < cutoff for expiry in self._expiries):
            return 0  # nothing delivered in between: the snapshot would rebuild identically
        calendars, native_hints = self._table
        before = self._resolver.snapshot.family_map
        snapshot = build_snapshot_from_calendar(
            calendars,
            today=cutoff,
            snapshot_ns=snapshot_ns,
            native_hints=native_hints,
        )
        changes = self._resolver.swap_snapshot(snapshot)
        for change in changes:
            logger.info(
                "contract_family_rolled",
                family=str(change.family),
                old_ref=(change.old_ref.display() if change.old_ref is not None else None),
                new_ref=(change.new_ref.display() if change.new_ref is not None else None),
                delivery_cutoff=cutoff.isoformat(),
            )
        # ``swap_snapshot`` reports a family that lost its binding as no change
        # at all, so a strategy on it would keep the settled contract silently.
        # There is no next month to rebind it to; say so.
        unbound = sorted(str(family) for family in before if family not in snapshot.family_map)
        if unbound:
            logger.warning(
                "contract_family_roll_left_unbound",
                families=unbound,
                delivery_cutoff=cutoff.isoformat(),
                note="no listed contract remains for these families; bound strategies keep the settled one",
            )
        return len(changes)

    async def run_roll_clock(self, *, interval_s: float = 60.0) -> None:
        """Call :meth:`roll_if_due` every ``interval_s`` for the life of the loop.

        A minute of lateness costs nothing: the settled contract no longer
        trades, so the strategy has nothing to do on it in the meantime.
        """
        while True:
            await asyncio.sleep(interval_s)
            try:
                self.roll_if_due()
            except Exception as exc:  # noqa: BLE001 - one bad pass must not end the clock
                logger.warning("contract_family_roll_check_failed", error=str(exc))
