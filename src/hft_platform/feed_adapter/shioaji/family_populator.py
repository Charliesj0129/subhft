"""Populate a :class:`ContractFamilyResolver` from a Shioaji contract table.

Reads ``api.Contracts.Futures.<root>`` and extracts every concrete expiry's
``delivery_month`` / ``delivery_date``, building a per-root list of
:class:`FutureRef`. The resolver's snapshot is then swapped atomically so
every consumer sees a consistent binding.

Design
------
* Runs **after** broker login fills the contract table (post-connect hook).
* Ignores R1/R2/C0/C1 alias entries — they carry no expiry of their own;
  the family binding itself is what this module builds.
* Failures in per-contract parsing are logged once and do not abort the
  whole refresh — one bad contract must not poison the binding table.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime
from typing import Any

from structlog import get_logger

from hft_platform.contracts.family_resolver import (
    ContractFamilyResolver,
    build_snapshot_from_calendar,
)
from hft_platform.contracts.ref import FutureRef
from hft_platform.core import timebase

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
        return {}, {}
    contracts = getattr(api, "Contracts", None)
    futures = getattr(contracts, "Futures", None) if contracts is not None else None
    if futures is None:
        return {}, {}
    try:
        roots = list(futures.keys())
    except Exception as exc:  # noqa: BLE001
        logger.debug("shioaji_family_populator_roots_failed", error=str(exc))
        return {}, {}

    out: dict[str, list[FutureRef]] = {}
    native_hints: dict[str, object] = {}
    for root in roots:
        try:
            contract_list = list(futures[root])
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "shioaji_family_populator_root_iter_failed",
                root=str(root),
                error=str(exc),
            )
            continue
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
    """
    if today is None:
        # P3-?: project rule "always `timebase.now_ns()`" applies even on
        # startup paths so the entire codebase has a single time source.
        today = datetime.fromtimestamp(timebase.now_ns() / 1e9, tz=UTC).date()
    calendars, native_hints = _extract_futures(api)
    snapshot = build_snapshot_from_calendar(
        calendars,
        today=today,
        snapshot_ns=timebase.now_ns(),
        native_hints=native_hints,
    )
    resolver.swap_snapshot(snapshot)
    logger.info(
        "shioaji_family_populator_snapshot_installed",
        roots=sorted(calendars.keys()),
        bindings=len(snapshot.family_map),
        native_hints=len(native_hints),
    )
    return len(snapshot.family_map)
