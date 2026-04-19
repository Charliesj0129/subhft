"""Populate a :class:`ContractFamilyResolver` from a Fubon symbol source.

Fubon's SDK does **not** expose ``delivery_month`` / ``delivery_date`` the way
Shioaji does — the canonical source of "which contracts exist" is the
YAML-backed :class:`FubonContractsRuntime`. This populator reads that
symbol list, parses each month-coded code through :func:`parse_display`
into a :class:`FutureRef`, groups by root, and swaps the resolver snapshot.

Design
------
* Accepts any of the following shapes for ``source``:
  - a raw ``list`` / ``tuple`` of symbol dicts (``{"code", "exchange"}``),
  - an object exposing a ``symbols`` attribute (``FubonContractsRuntime``),
  - an object exposing ``_contracts_runtime.symbols`` (``FubonClientFacade``),
  - ``None`` (noop, returns 0).
* Non-future codes (stocks, options, alias tokens) are **silently** dropped;
  this module is only concerned with futures families.
* Idempotent — repeat calls on unchanged inputs produce no rebinds.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Iterable

from structlog import get_logger

from hft_platform.contracts.family_resolver import (
    ContractFamilyResolver,
    build_snapshot_from_calendar,
)
from hft_platform.contracts.ref import FutureRef, parse_display
from hft_platform.core import timebase

logger = get_logger("feed_adapter.fubon.family_populator")


def _get_symbols(source: Any) -> list[Any]:
    """Extract a symbol list from a raw list, a runtime, or a facade."""
    if source is None:
        return []
    if isinstance(source, (list, tuple)):
        return list(source)
    symbols = getattr(source, "symbols", None)
    if symbols is not None:
        try:
            return list(symbols)
        except TypeError:
            return []
    inner = getattr(source, "_contracts_runtime", None)
    if inner is not None and inner is not source:
        return _get_symbols(inner)
    return []


def _extract_code(entry: Any) -> str | None:
    if isinstance(entry, dict):
        code = entry.get("code") or entry.get("symbol")
    else:
        code = getattr(entry, "code", None) or getattr(entry, "symbol", None)
    if code is None:
        return None
    text = str(code).strip()
    return text or None


def _extract_futures(codes: Iterable[str], *, base_year: int) -> dict[str, list[FutureRef]]:
    """Parse codes, keep futures only, group by root."""
    out: dict[str, list[FutureRef]] = {}
    for code in codes:
        try:
            parsed = parse_display(code, base_year=base_year)
        except ValueError:
            continue
        if not isinstance(parsed, FutureRef):
            continue
        out.setdefault(parsed.root, []).append(parsed)
    return out


def populate_resolver_from_fubon(
    resolver: ContractFamilyResolver,
    source: Any,
    *,
    today: date | None = None,
) -> int:
    """Swap ``resolver``'s snapshot to one derived from the Fubon symbol list.

    Returns the number of family bindings installed. Safe to call repeatedly.
    """
    if today is None:
        today = datetime.now(UTC).date()

    raw = _get_symbols(source)
    if not raw:
        logger.debug("fubon_family_populator_empty_source")
        return 0

    codes: list[str] = []
    for entry in raw:
        code = _extract_code(entry)
        if code is not None:
            codes.append(code)

    calendars = _extract_futures(codes, base_year=today.year)
    snapshot = build_snapshot_from_calendar(
        calendars,
        today=today,
        snapshot_ns=timebase.now_ns(),
    )
    resolver.swap_snapshot(snapshot)
    logger.info(
        "fubon_family_populator_snapshot_installed",
        roots=sorted(calendars.keys()),
        bindings=len(snapshot.family_map),
    )
    return len(snapshot.family_map)
