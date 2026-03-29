"""Canonical instrument metadata for futures, options, and equities.

InstrumentProfile is the single source of truth for all per-symbol static
properties (tick size, fee structure, trading hours, contract multiplier).
It replaces flat ``symbol: str`` + ad-hoc dict lookups throughout the platform.

All monetary fields follow the Precision Law: scaled integers (x10000).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import Literal

import structlog


class InstrumentType(enum.Enum):
    """Top-level instrument classification."""

    FUTURE = "future"
    OPTION = "option"
    EQUITY = "equity"
    INDEX = "index"


class OptionRight(enum.Enum):
    """Option contract right."""

    CALL = "C"
    PUT = "P"


@dataclass(frozen=True, slots=True)
class FeeStructure:
    """Exchange and broker fee parameters for a single instrument.

    All monetary fields are scaled integers (x10000) per the Precision Law.
    """

    tax_rate_bps: int
    """Transaction tax in basis points (e.g. 20 = 0.20 bps)."""

    commission_per_lot: int
    """Broker commission per lot, scaled x10000."""


@dataclass(frozen=True, slots=True)
class TradingHours:
    """Day and night session boundaries (HH:MM strings, Taiwan local time).

    ``night_open`` / ``night_close`` are ``None`` for instruments without a
    night session (e.g. TWSE equities, mini-TAIEX options).
    """

    day_open: str
    day_close: str
    night_open: str | None
    night_close: str | None


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    """Immutable metadata record for a single tradable instrument.

    Designed for O(1) lookup via a symbol-keyed dict.  All price fields use
    scaled integers (x10000) consistent with the platform-wide Precision Law.
    Options-only fields (``strike_scaled``, ``option_right``, ``expiry``) are
    ``None`` for non-option instruments.
    """

    symbol: str
    """Canonical symbol string (e.g. 'TXFC0', 'TXO22000C202604', '2330')."""

    instrument_type: InstrumentType

    underlying: str
    """Root product (e.g. 'TX' for TAIEX futures/options, '2330' for TSMC)."""

    exchange: str
    """Exchange identifier (e.g. 'TAIFEX', 'TWSE')."""

    multiplier: int
    """Contract value multiplier (NTD per index point or per share lot)."""

    tick_size_scaled: int
    """Minimum price increment, scaled x10000 (e.g. 10000 = 1 index point)."""

    price_scale: int
    """Price scale factor applied to raw broker prices (typically 10000)."""

    fee_structure: FeeStructure

    trading_hours: TradingHours

    lot_size: int = 1
    """TAIFEX: 1 lot = 1 contract.  TWSE: 1 lot = 1000 shares."""

    # Options-only fields — None for non-option instruments
    strike_scaled: int | None = None
    """Strike price scaled x10000."""

    option_right: OptionRight | None = None

    expiry: date | None = None
    """Contract expiry date."""


# ---------------------------------------------------------------------------
# InstrumentRegistry
# ---------------------------------------------------------------------------

_log = structlog.get_logger(__name__)


class InstrumentLimitError(Exception):
    """Raised when instrument cardinality limit is exceeded."""


class InstrumentRegistry:
    """Thread-unsafe, in-process registry of :class:`InstrumentProfile` records.

    Supports two source tags:

    * ``"static"`` — loaded from config/YAML at startup.  Cleared and replaced
      atomically by :meth:`reload_static`.
    * ``"dynamic"`` — discovered at runtime (e.g. options chain expansion).
      Preserved across :meth:`reload_static` calls.

    Cardinality is bounded by *max_instruments*.  When the registry is full,
    :meth:`register` attempts to evict expired profiles before raising
    :class:`InstrumentLimitError`.
    """

    __slots__ = ("_profiles", "_sources", "_by_underlying", "_max")

    def __init__(self, max_instruments: int = 5000) -> None:
        self._profiles: dict[str, InstrumentProfile] = {}
        self._sources: dict[str, str] = {}  # symbol -> "static" | "dynamic"
        self._by_underlying: dict[str, list[str]] = {}
        self._max: int = max_instruments

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def register(
        self,
        profile: InstrumentProfile,
        *,
        source: Literal["static", "dynamic"] = "static",
    ) -> None:
        """Add or update *profile* in the registry.

        If the symbol already exists it is removed first (update semantics).
        If the registry is at capacity, expired profiles are evicted before
        raising :class:`InstrumentLimitError`.
        """
        symbol = profile.symbol
        existing = symbol in self._profiles
        if existing:
            self._remove(symbol)

        if len(self._profiles) >= self._max:
            self._try_evict_for_space(symbol)

        self._profiles[symbol] = profile
        self._sources[symbol] = source
        underlying = profile.underlying
        if underlying not in self._by_underlying:
            self._by_underlying[underlying] = []
        self._by_underlying[underlying].append(symbol)

    def bulk_register(
        self,
        profiles: list[InstrumentProfile],
        *,
        source: Literal["static", "dynamic"] = "static",
    ) -> None:
        """Register multiple profiles in one call."""
        for profile in profiles:
            self.register(profile, source=source)

    def evict_expired(self, as_of: date) -> int:
        """Remove all profiles whose ``expiry`` is strictly before *as_of*.

        Returns the number of evicted profiles.  Each eviction is logged at
        WARNING level.
        """
        to_evict = [
            sym
            for sym, prof in self._profiles.items()
            if prof.expiry is not None and prof.expiry < as_of
        ]
        for sym in to_evict:
            _log.warning(
                "evicting_expired_instrument",
                symbol=sym,
                expiry=str(self._profiles[sym].expiry),
                as_of=str(as_of),
            )
            self._remove(sym)
        return len(to_evict)

    def reload_static(self, profiles: list[InstrumentProfile]) -> None:
        """Replace all ``"static"`` profiles atomically.

        Dynamic profiles are preserved unchanged.
        """
        static_symbols = [s for s, src in self._sources.items() if src == "static"]
        for sym in static_symbols:
            self._remove(sym)
        for profile in profiles:
            self.register(profile, source="static")

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get(self, symbol: str) -> InstrumentProfile:
        """Return profile for *symbol*, raising :class:`KeyError` if absent."""
        return self._profiles[symbol]

    def contains(self, symbol: str) -> bool:
        """Return ``True`` if *symbol* is registered."""
        return symbol in self._profiles

    def get_by_underlying(self, underlying: str) -> list[InstrumentProfile]:
        """Return all profiles whose ``underlying`` matches."""
        symbols = self._by_underlying.get(underlying, [])
        return [self._profiles[s] for s in symbols if s in self._profiles]

    def get_options_chain(
        self, underlying: str, expiry: date
    ) -> list[InstrumentProfile]:
        """Return all OPTION profiles for *underlying* expiring on *expiry*."""
        return [
            p
            for p in self.get_by_underlying(underlying)
            if p.instrument_type is InstrumentType.OPTION and p.expiry == expiry
        ]

    @property
    def size(self) -> int:
        """Number of registered profiles."""
        return len(self._profiles)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _remove(self, symbol: str) -> None:
        """Remove *symbol* from all internal collections."""
        profile = self._profiles.pop(symbol, None)
        self._sources.pop(symbol, None)
        if profile is not None:
            underlying_list = self._by_underlying.get(profile.underlying)
            if underlying_list is not None:
                try:
                    underlying_list.remove(symbol)
                except ValueError:
                    pass
                if not underlying_list:
                    del self._by_underlying[profile.underlying]

    def _try_evict_for_space(self, requesting_symbol: str) -> None:
        """Attempt to free space by evicting expired profiles.

        Raises :class:`InstrumentLimitError` if nothing could be evicted.
        """
        evicted = self.evict_expired(as_of=date.today())
        if evicted == 0:
            raise InstrumentLimitError(
                f"InstrumentRegistry is at capacity ({self._max}); "
                f"cannot register '{requesting_symbol}'. "
                "No expired profiles available for eviction."
            )
