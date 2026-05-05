"""Structured contract identity (ContractRef) — canonical form at the type level.

Gate 0 of the Option-3 migration (see docs/architecture/ when spec lands).

Eliminates primitive-obsession across the platform where 'which contract' was
represented as bare str and drifted between consumers (``alias_to_actual`` dict
in ``SymbolMetadata`` / client / OrderAdapter; ``event.symbol`` raw vs resolved;
ExposureStore key fragmentation, etc.).

Design
------
- Sum type: ``FutureRef | OptionRef | StockRef``. Wrong field access fails at
  type-check time.
- ``ContractFamily`` represents a continuous-contract reference (TMF:R1) that
  resolver binds to a concrete ``FutureRef`` at snapshot-swap time.
- ``display()`` returns the canonical platform string (Shioaji-compatible
  month-letter form for futures). Persistence sinks (ClickHouse, Prometheus,
  WAL, alerts) take this string and nothing else.
- Broker-specific wire translation (Shioaji ``Contract`` object, Fubon str
  symbol) happens inside each ``ContractResolver`` implementation.

Hot-path note
-------------
``ContractRef`` is a frozen dataclass with slots — allocation is cheap but still
present. On the hot path, use the interned ``symbol_id: int`` from
``rust_core::SymbolInternTable``; ContractRef materializes only in cold planes
(strategy registration, risk config, recorder output).
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

__all__ = [
    "Product",
    "Right",
    "FamilyCode",
    "FutureRef",
    "OptionRef",
    "StockRef",
    "ContractRef",
    "ContractFamily",
    "ContractResolver",
    "ImmutableContractSnapshot",
    "MONTH_LETTERS",
    "parse_display",
]


# Shioaji month-letter convention: Jan=A .. Dec=L. Referenced by
# src/hft_platform/feed_adapter/shioaji/contracts_runtime.py:25 so
# ContractRef.display() round-trips through the broker wire form.
MONTH_LETTERS: str = "ABCDEFGHIJKL"


class Product(str, Enum):
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    STOCK = "STOCK"


class Right(str, Enum):
    CALL = "C"
    PUT = "P"


class FamilyCode(str, Enum):
    R1 = "R1"  # Nearest non-expired
    R2 = "R2"  # Second-nearest non-expired
    SPECIFIC = "SPECIFIC"  # Absolute expiry — carried via the ref's ``expiry`` field


@dataclass(frozen=True, slots=True)
class FutureRef:
    """Concrete futures contract identity.

    ``family`` records the source form (R1 family vs. a pinned month) so logs
    and observability can trace whether a given binding originated from a
    continuous-contract reference or an explicit expiry.
    """

    root: str
    expiry: date
    family: FamilyCode = FamilyCode.SPECIFIC

    def display(self) -> str:
        """Canonical platform form: root + month-letter + year-last-digit.

        Example: ``FutureRef("TMF", date(2026, 5, 21))`` → ``"TMFE6"``.
        """
        letter = MONTH_LETTERS[self.expiry.month - 1]
        year_digit = self.expiry.year % 10
        return f"{self.root}{letter}{year_digit}"


@dataclass(frozen=True, slots=True)
class OptionRef:
    """Concrete options contract identity.

    ``strike`` is the raw integer strike (whole points, not x10000-scaled).
    """

    root: str
    expiry: date
    strike: int
    right: Right

    def display(self) -> str:
        """Canonical form: ``root + yyyymm + right_letter + strike``.

        Example: ``OptionRef("TXO", date(2026, 5, 21), 23000, Right.CALL)``
        → ``"TXO202605C23000"``.
        """
        return f"{self.root}{self.expiry.year:04d}{self.expiry.month:02d}{self.right.value}{self.strike}"


@dataclass(frozen=True, slots=True)
class StockRef:
    code: str

    def display(self) -> str:
        return self.code


ContractRef = FutureRef | OptionRef | StockRef


@dataclass(frozen=True, slots=True)
class ContractFamily:
    """Reference to a continuous-contract family (e.g., ``TMF:R1``).

    Used in strategy config (``config/live/strategies.yaml``) so a strategy can
    declare "I trade the nearest TMF" without pinning an expiry. Runtime
    resolver binds ``ContractFamily`` → concrete ``FutureRef`` / ``OptionRef``
    at snapshot-swap time; binding is observable via ``FamilyBindingChanged``
    events when the underlying expiry rolls.
    """

    product: Product
    root: str
    family: FamilyCode

    def __post_init__(self) -> None:
        if self.product is Product.STOCK:
            raise ValueError("ContractFamily is not applicable to stocks")
        if self.family is FamilyCode.SPECIFIC:
            raise ValueError("ContractFamily requires R1/R2 (not SPECIFIC); use a concrete ref instead")


@runtime_checkable
class ContractResolver(Protocol):
    """Broker-agnostic contract resolver.

    Implementations live next to each broker adapter:
    - ``ShioajiResolver``: walks ``api.Contracts.Futures.<root>`` sorted by
      ``delivery_month`` to bind R1/R2 families.
    - ``FubonResolver``: consults our own expiry calendar because Fubon SDK
      does not expose ``delivery_month`` (see
      ``feed_adapter/fubon/contracts_runtime.py``).
    """

    def resolve_family(self, family: ContractFamily) -> ContractRef | None:
        """Bind a family reference to the currently-active concrete ref."""

    def to_native(self, ref: ContractRef) -> object:
        """Translate a ref to the broker's native contract object or wire string."""

    def from_callback_symbol(self, raw_symbol: str) -> ContractRef | None:
        """Parse a broker-callback raw symbol back to a typed ref."""


@dataclass(frozen=True, slots=True)
class ImmutableContractSnapshot:
    """Atomically-swappable contract binding table.

    Invariants:
    - Constructed via :meth:`build`; inner maps wrapped in ``MappingProxyType``.
    - Consumers hold a reference for the duration of a tick/event so reads are
      consistent; resolver swaps the whole snapshot at well-defined points
      (startup, reconnect, rollover).
    """

    family_map: Mapping[ContractFamily, ContractRef]
    native_hints: Mapping[str, object]
    snapshot_ns: int = field(default=0)

    @classmethod
    def build(
        cls,
        family_map: dict[ContractFamily, ContractRef],
        native_hints: dict[str, object],
        snapshot_ns: int,
    ) -> ImmutableContractSnapshot:
        return cls(
            family_map=MappingProxyType(dict(family_map)),
            native_hints=MappingProxyType(dict(native_hints)),
            snapshot_ns=snapshot_ns,
        )

    def resolve_family(self, family: ContractFamily) -> ContractRef | None:
        return self.family_map.get(family)

    def native_hint(self, ref: ContractRef) -> object | None:
        return self.native_hints.get(ref.display())


_FUTURE_FAMILY_RE = re.compile(r"^([A-Z]{2,4})(R[12])$")
_FUTURE_MONTH_RE = re.compile(r"^([A-Z]{2,4})([A-L])([0-9])$")
_OPTION_RE = re.compile(r"^([A-Z]{2,4})(\d{4})(\d{2})([CP])(\d+)$")
_STOCK_RE = re.compile(r"^\d{3,6}[A-Za-z]?$")


def parse_display(s: str, base_year: int) -> ContractRef | ContractFamily:
    """Parse a canonical display string back to a typed ref.

    Used during the dual-write migration phase where config YAML still stores
    strings. ``base_year`` anchors the decade for single-digit future years:
    ``parse_display("TMFE6", base_year=2026)`` → May 2026.

    The future single-digit year is resolved to the nearest decade boundary
    such that the resulting year is not more than 5 years in the past relative
    to ``base_year``; otherwise it wraps forward by one decade.
    """
    if match := _FUTURE_FAMILY_RE.match(s):
        root, fam_str = match.groups()
        return ContractFamily(product=Product.FUTURE, root=root, family=FamilyCode(fam_str))

    if match := _FUTURE_MONTH_RE.match(s):
        root, letter, year_digit = match.groups()
        month = MONTH_LETTERS.index(letter) + 1
        year = (base_year // 10) * 10 + int(year_digit)
        if year < base_year - 5:
            year += 10
        last_day = monthrange(year, month)[1]
        return FutureRef(root=root, expiry=date(year, month, last_day))

    if match := _OPTION_RE.match(s):
        root, yyyy, mm, right_str, strike_str = match.groups()
        year = int(yyyy)
        month = int(mm)
        last_day = monthrange(year, month)[1]
        return OptionRef(
            root=root,
            expiry=date(year, month, last_day),
            strike=int(strike_str),
            right=Right(right_str),
        )

    if _STOCK_RE.match(s):
        return StockRef(code=s)

    raise ValueError(f"Cannot parse contract display: {s!r}")
