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
