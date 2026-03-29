# Multi-Instrument Phase 1: Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `InstrumentRegistry` as the canonical instrument metadata store, wrap `SymbolMetadata` for backward compatibility, extend ClickHouse schema and recorder pipeline for multi-instrument columns, and clean up dead feature code (vrr).

**Architecture:** `InstrumentRegistry` is a new singleton module (`src/hft_platform/core/instrument_registry.py`) that holds `InstrumentProfile` frozen dataclasses keyed by symbol string. `SymbolMetadata` becomes a thin delegating wrapper. ClickHouse gets 5 additive columns via migration. Recorder `worker.py` and `_loader_batch.py` column lists are extended in sync. No event structure changes.

**Tech Stack:** Python 3.12, dataclasses (frozen, slots), ClickHouse ALTER TABLE, pytest

**Spec:** `docs/superpowers/specs/2026-03-29-multi-instrument-infrastructure-design.md` — Sections 2 (InstrumentRegistry), 5.1-5.5 (ClickHouse/Recorder)

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| **Create** | `src/hft_platform/core/instrument_registry.py` | InstrumentProfile, InstrumentType, FeeStructure, TradingHours, InstrumentRegistry class |
| **Create** | `tests/unit/test_instrument_registry.py` | Unit tests for registry CRUD, cardinality guard, eviction, compat wrapper |
| **Modify** | `src/hft_platform/feed_adapter/normalizer.py:117-289` | SymbolMetadata delegates to InstrumentRegistry |
| **Create** | `src/hft_platform/migrations/clickhouse/20260330_001_add_instrument_columns.sql` | DDL: 5 columns on market_data, 2 on orders, 2 on fills |
| **Modify** | `src/hft_platform/recorder/worker.py:16-29` | Extend MARKET_DATA_COLUMNS with 5 instrument fields |
| **Modify** | `src/hft_platform/recorder/_loader_batch.py:23-36` | Extend _MARKET_DATA_COLS in sync |
| **Modify** | `src/hft_platform/recorder/mapper.py:78-167` | Populate instrument metadata in record dicts |
| **Create** | `tests/unit/test_instrument_registry_compat.py` | Verify SymbolMetadata wrapper covers all 8 methods + 2 attrs |
| **Modify** | `src/hft_platform/feature/registry.py:197-221` | Remove or register vrr (cleanup dead code) |

---

### Task 1: InstrumentProfile and InstrumentType Data Structures

**Files:**
- Create: `src/hft_platform/core/instrument_registry.py`
- Test: `tests/unit/test_instrument_registry.py`

- [ ] **Step 1: Write failing tests for data structures**

```python
# tests/unit/test_instrument_registry.py
"""Unit tests for InstrumentRegistry core data structures."""
from __future__ import annotations

import pytest
from datetime import date

from hft_platform.core.instrument_registry import (
    InstrumentType,
    OptionRight,
    FeeStructure,
    TradingHours,
    InstrumentProfile,
)


class TestInstrumentProfile:
    def test_futures_profile_construction(self):
        fee = FeeStructure(tax_rate_bps=20, commission_per_lot=130000)
        hours = TradingHours(
            day_open="08:45", day_close="13:45",
            night_open="15:00", night_close="05:00",
        )
        profile = InstrumentProfile(
            symbol="TXFC0",
            instrument_type=InstrumentType.FUTURE,
            underlying="TX",
            exchange="TAIFEX",
            multiplier=200,
            tick_size_scaled=10000,
            price_scale=10000,
            fee_structure=fee,
            trading_hours=hours,
        )
        assert profile.symbol == "TXFC0"
        assert profile.instrument_type == InstrumentType.FUTURE
        assert profile.underlying == "TX"
        assert profile.multiplier == 200
        assert profile.strike_scaled is None
        assert profile.option_right is None
        assert profile.expiry is None
        assert profile.lot_size == 1

    def test_option_profile_construction(self):
        fee = FeeStructure(tax_rate_bps=20, commission_per_lot=130000)
        hours = TradingHours(
            day_open="08:45", day_close="13:45",
            night_open=None, night_close=None,
        )
        profile = InstrumentProfile(
            symbol="TXO22000C202604",
            instrument_type=InstrumentType.OPTION,
            underlying="TX",
            exchange="TAIFEX",
            multiplier=50,
            tick_size_scaled=10000,
            price_scale=10000,
            fee_structure=fee,
            trading_hours=hours,
            strike_scaled=220000000,
            option_right=OptionRight.CALL,
            expiry=date(2026, 4, 15),
        )
        assert profile.instrument_type == InstrumentType.OPTION
        assert profile.strike_scaled == 220000000
        assert profile.option_right == OptionRight.CALL
        assert profile.expiry == date(2026, 4, 15)

    def test_equity_profile_construction(self):
        fee = FeeStructure(tax_rate_bps=30, commission_per_lot=0)
        hours = TradingHours(
            day_open="09:00", day_close="13:30",
            night_open=None, night_close=None,
        )
        profile = InstrumentProfile(
            symbol="2330",
            instrument_type=InstrumentType.EQUITY,
            underlying="2330",
            exchange="TWSE",
            multiplier=1000,
            tick_size_scaled=5000,
            price_scale=10000,
            fee_structure=fee,
            trading_hours=hours,
            lot_size=1000,
        )
        assert profile.lot_size == 1000
        assert profile.multiplier == 1000

    def test_profile_is_frozen(self):
        fee = FeeStructure(tax_rate_bps=20, commission_per_lot=130000)
        hours = TradingHours(day_open="08:45", day_close="13:45", night_open=None, night_close=None)
        profile = InstrumentProfile(
            symbol="TXFC0", instrument_type=InstrumentType.FUTURE,
            underlying="TX", exchange="TAIFEX", multiplier=200,
            tick_size_scaled=10000, price_scale=10000,
            fee_structure=fee, trading_hours=hours,
        )
        with pytest.raises(AttributeError):
            profile.symbol = "OTHER"  # type: ignore[misc]

    def test_instrument_type_values(self):
        assert InstrumentType.FUTURE.value == "future"
        assert InstrumentType.OPTION.value == "option"
        assert InstrumentType.EQUITY.value == "equity"
        assert InstrumentType.INDEX.value == "index"

    def test_option_right_values(self):
        assert OptionRight.CALL.value == "C"
        assert OptionRight.PUT.value == "P"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_instrument_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.core.instrument_registry'`

- [ ] **Step 3: Implement data structures**

```python
# src/hft_platform/core/instrument_registry.py
"""Canonical instrument metadata registry for multi-instrument support.

Replaces SymbolMetadata as the single source of truth for per-instrument
configuration: type, scaling, fees, trading hours, and options-specific fields.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class InstrumentType(enum.Enum):
    FUTURE = "future"
    OPTION = "option"
    EQUITY = "equity"
    INDEX = "index"


class OptionRight(enum.Enum):
    CALL = "C"
    PUT = "P"


@dataclass(frozen=True, slots=True)
class FeeStructure:
    tax_rate_bps: int
    commission_per_lot: int  # scaled x10000


@dataclass(frozen=True, slots=True)
class TradingHours:
    day_open: str
    day_close: str
    night_open: str | None
    night_close: str | None


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    symbol: str
    instrument_type: InstrumentType
    underlying: str
    exchange: str
    multiplier: int
    tick_size_scaled: int  # x10000
    price_scale: int
    fee_structure: FeeStructure
    trading_hours: TradingHours
    lot_size: int = 1
    # Options-only (None for non-options)
    strike_scaled: int | None = None
    option_right: OptionRight | None = None
    expiry: date | None = None
```

- [ ] **Step 4: Ensure `src/hft_platform/core/__init__.py` exists**

Check if `src/hft_platform/core/__init__.py` exists. If not, it likely already does (pricing.py is there). Verify:

Run: `ls src/hft_platform/core/__init__.py`

If missing, create an empty `__init__.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_instrument_registry.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/core/instrument_registry.py tests/unit/test_instrument_registry.py
git commit -m "feat(core): add InstrumentProfile and InstrumentType data structures"
```

---

### Task 2: InstrumentRegistry CRUD and Cardinality Guard

**Files:**
- Modify: `src/hft_platform/core/instrument_registry.py`
- Test: `tests/unit/test_instrument_registry.py`

- [ ] **Step 1: Write failing tests for registry operations**

Append to `tests/unit/test_instrument_registry.py`:

```python
from hft_platform.core.instrument_registry import InstrumentRegistry, InstrumentLimitError


def _make_future(symbol: str = "TXFC0", underlying: str = "TX") -> InstrumentProfile:
    return InstrumentProfile(
        symbol=symbol,
        instrument_type=InstrumentType.FUTURE,
        underlying=underlying,
        exchange="TAIFEX",
        multiplier=200,
        tick_size_scaled=10000,
        price_scale=10000,
        fee_structure=FeeStructure(tax_rate_bps=20, commission_per_lot=130000),
        trading_hours=TradingHours(day_open="08:45", day_close="13:45", night_open=None, night_close=None),
    )


def _make_option(
    symbol: str = "TXO22000C202604",
    underlying: str = "TX",
    strike: int = 220000000,
    right: OptionRight = OptionRight.CALL,
    expiry: date = date(2026, 4, 15),
) -> InstrumentProfile:
    return InstrumentProfile(
        symbol=symbol,
        instrument_type=InstrumentType.OPTION,
        underlying=underlying,
        exchange="TAIFEX",
        multiplier=50,
        tick_size_scaled=10000,
        price_scale=10000,
        fee_structure=FeeStructure(tax_rate_bps=20, commission_per_lot=130000),
        trading_hours=TradingHours(day_open="08:45", day_close="13:45", night_open=None, night_close=None),
        strike_scaled=strike,
        option_right=right,
        expiry=expiry,
    )


class TestInstrumentRegistry:
    def test_register_and_get(self):
        reg = InstrumentRegistry(max_instruments=100)
        profile = _make_future()
        reg.register(profile, source="static")
        assert reg.get("TXFC0") is profile

    def test_get_missing_raises_keyerror(self):
        reg = InstrumentRegistry(max_instruments=100)
        with pytest.raises(KeyError):
            reg.get("NONEXISTENT")

    def test_contains(self):
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(_make_future(), source="static")
        assert reg.contains("TXFC0")
        assert not reg.contains("NONEXISTENT")

    def test_get_by_underlying(self):
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(_make_future("TXFC0", "TX"), source="static")
        reg.register(_make_future("TXFC1", "TX"), source="static")
        reg.register(_make_future("MXFC0", "MTX"), source="static")
        result = reg.get_by_underlying("TX")
        assert {p.symbol for p in result} == {"TXFC0", "TXFC1"}

    def test_get_options_chain(self):
        reg = InstrumentRegistry(max_instruments=100)
        exp = date(2026, 4, 15)
        reg.register(_make_option("TXO22000C202604", "TX", 220000000, OptionRight.CALL, exp), source="dynamic")
        reg.register(_make_option("TXO22500P202604", "TX", 225000000, OptionRight.PUT, exp), source="dynamic")
        reg.register(_make_option("TXO22000C202605", "TX", 220000000, OptionRight.CALL, date(2026, 5, 21)), source="dynamic")
        chain = reg.get_options_chain("TX", exp)
        assert len(chain) == 2
        assert all(p.expiry == exp for p in chain)

    def test_bulk_register(self):
        reg = InstrumentRegistry(max_instruments=100)
        profiles = [_make_future(f"TXF{i}", "TX") for i in range(5)]
        reg.bulk_register(profiles, source="static")
        assert len(list(reg.get_by_underlying("TX"))) == 5

    def test_evict_expired(self):
        reg = InstrumentRegistry(max_instruments=100)
        past = date(2026, 3, 1)
        future_exp = date(2026, 5, 1)
        reg.register(_make_option("EXPIRED1", "TX", 220000000, OptionRight.CALL, past), source="dynamic")
        reg.register(_make_option("ACTIVE1", "TX", 220000000, OptionRight.CALL, future_exp), source="dynamic")
        evicted = reg.evict_expired(as_of=date(2026, 4, 1))
        assert evicted == 1
        assert not reg.contains("EXPIRED1")
        assert reg.contains("ACTIVE1")

    def test_cardinality_guard_rejects_over_limit(self):
        reg = InstrumentRegistry(max_instruments=3)
        for i in range(3):
            reg.register(_make_future(f"F{i}", "TX"), source="static")
        with pytest.raises(InstrumentLimitError):
            reg.register(_make_future("F3", "TX"), source="static")

    def test_cardinality_guard_evicts_expired_first(self):
        reg = InstrumentRegistry(max_instruments=3)
        reg.register(_make_option("EXP1", "TX", 220000000, OptionRight.CALL, date(2026, 1, 1)), source="dynamic")
        reg.register(_make_future("F1", "TX"), source="static")
        reg.register(_make_future("F2", "TX"), source="static")
        # At limit (3). Register another — should evict expired option first.
        reg.register(_make_future("F3", "TX"), source="static")
        assert not reg.contains("EXP1")
        assert reg.contains("F3")

    def test_static_reload_preserves_dynamic(self):
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(_make_future("TXFC0", "TX"), source="static")
        reg.register(_make_option("TXO22000C202604", "TX", 220000000, OptionRight.CALL, date(2026, 4, 15)), source="dynamic")
        # Simulate static reload — clear static, re-register
        reg.reload_static([_make_future("TXFC0_NEW", "TX")])
        assert not reg.contains("TXFC0")
        assert reg.contains("TXFC0_NEW")
        assert reg.contains("TXO22000C202604")  # dynamic preserved
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_instrument_registry.py::TestInstrumentRegistry -v`
Expected: FAIL with `ImportError: cannot import name 'InstrumentRegistry'`

- [ ] **Step 3: Implement InstrumentRegistry**

Append to `src/hft_platform/core/instrument_registry.py`:

```python
import structlog
from datetime import date as _date_type
from typing import Iterable, Literal

logger = structlog.get_logger(__name__)


class InstrumentLimitError(Exception):
    """Raised when instrument cardinality limit is exceeded."""


class InstrumentRegistry:
    """Canonical singleton registry for all instrument metadata.

    Each profile is tagged with source="static" (from symbols.yaml)
    or source="dynamic" (from ContractsRuntime or first-seen callback).
    reload_static() only clears static entries, preserving dynamic ones.
    """

    __slots__ = ("_profiles", "_sources", "_by_underlying", "_max")

    def __init__(self, max_instruments: int = 5000) -> None:
        self._profiles: dict[str, InstrumentProfile] = {}
        self._sources: dict[str, str] = {}  # symbol -> "static" | "dynamic"
        self._by_underlying: dict[str, list[str]] = {}  # underlying -> [symbol, ...]
        self._max: int = max_instruments

    def register(
        self,
        profile: InstrumentProfile,
        *,
        source: Literal["static", "dynamic"] = "static",
    ) -> None:
        if profile.symbol in self._profiles:
            self._remove(profile.symbol)
        elif len(self._profiles) >= self._max:
            self._try_evict_for_space(profile.symbol)
        self._profiles[profile.symbol] = profile
        self._sources[profile.symbol] = source
        self._by_underlying.setdefault(profile.underlying, []).append(profile.symbol)

    def bulk_register(
        self,
        profiles: Iterable[InstrumentProfile],
        *,
        source: Literal["static", "dynamic"] = "static",
    ) -> None:
        for p in profiles:
            self.register(p, source=source)

    def get(self, symbol: str) -> InstrumentProfile:
        return self._profiles[symbol]

    def contains(self, symbol: str) -> bool:
        return symbol in self._profiles

    def get_by_underlying(self, underlying: str) -> list[InstrumentProfile]:
        syms = self._by_underlying.get(underlying, [])
        return [self._profiles[s] for s in syms if s in self._profiles]

    def get_options_chain(
        self, underlying: str, expiry: _date_type,
    ) -> list[InstrumentProfile]:
        return [
            p for p in self.get_by_underlying(underlying)
            if p.instrument_type == InstrumentType.OPTION and p.expiry == expiry
        ]

    def evict_expired(self, as_of: _date_type) -> int:
        expired = [
            sym for sym, p in self._profiles.items()
            if p.expiry is not None and p.expiry < as_of
        ]
        for sym in expired:
            self._remove(sym)
            logger.warning("evicted_expired_instrument", symbol=sym)
        return len(expired)

    def reload_static(self, profiles: Iterable[InstrumentProfile]) -> None:
        """Re-load static profiles from symbols.yaml. Preserves dynamic profiles."""
        static_syms = [s for s, src in self._sources.items() if src == "static"]
        for sym in static_syms:
            self._remove(sym)
        for p in profiles:
            self.register(p, source="static")

    @property
    def size(self) -> int:
        return len(self._profiles)

    def _remove(self, symbol: str) -> None:
        profile = self._profiles.pop(symbol, None)
        self._sources.pop(symbol, None)
        if profile is not None:
            ul_list = self._by_underlying.get(profile.underlying, [])
            if symbol in ul_list:
                ul_list.remove(symbol)

    def _try_evict_for_space(self, requesting_symbol: str) -> None:
        from datetime import date as _d
        today = _d.today()
        evicted = self.evict_expired(as_of=today)
        if evicted > 0:
            return
        raise InstrumentLimitError(
            f"Registry at capacity ({self._max}), no expired instruments to evict. "
            f"Cannot register {requesting_symbol}."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_instrument_registry.py -v`
Expected: All 16 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/core/instrument_registry.py tests/unit/test_instrument_registry.py
git commit -m "feat(core): add InstrumentRegistry with CRUD, cardinality guard, static/dynamic source tagging"
```

---

### Task 3: SymbolMetadata Backward-Compat Wrapper

**Files:**
- Modify: `src/hft_platform/feed_adapter/normalizer.py:117-289`
- Create: `tests/unit/test_instrument_registry_compat.py`

This task makes `SymbolMetadata` delegate to `InstrumentRegistry` while preserving the exact public API surface (8 methods + 2 attributes + 1 class constant). The wrapper must be drop-in compatible so that all 10+ instantiation sites and their callers continue to work.

- [ ] **Step 1: Write failing tests for wrapper compatibility**

```python
# tests/unit/test_instrument_registry_compat.py
"""Verify SymbolMetadata backward-compat wrapper covers full public API."""
from __future__ import annotations

import os
import tempfile
import pytest
import yaml

from hft_platform.feed_adapter.normalizer import SymbolMetadata


@pytest.fixture
def symbols_yaml(tmp_path):
    """Create a minimal symbols.yaml for testing."""
    data = {
        "symbols": [
            {
                "code": "TXFC0",
                "exchange": "FUT",
                "tags": ["futures", "front_month", "txf"],
                "point_value": 200,
                "tick_size": 1.0,
            },
            {
                "code": "2330",
                "exchange": "TSE",
                "tags": ["stocks", "tw50"],
                "tick_size": 0.5,
            },
            {
                "code": "TXO22000C202604",
                "exchange": "OPT",
                "tags": ["options", "txo"],
                "product_type": "option",
                "point_value": 50,
            },
        ],
    }
    path = tmp_path / "symbols.yaml"
    path.write_text(yaml.dump(data))
    return str(path)


class TestSymbolMetadataCompat:
    """All 8 public methods + 2 attributes + 1 class constant."""

    def test_price_scale(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        # tick_size=1.0 → scale=1, but DEFAULT_SCALE fallback is 10000
        assert isinstance(meta.price_scale("TXFC0"), int)

    def test_contract_multiplier(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert meta.contract_multiplier("TXFC0") == 200
        assert meta.contract_multiplier("2330") == 1  # no point_value → default 1

    def test_exchange(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert meta.exchange("TXFC0") == "FUT"
        assert meta.exchange("2330") == "TSE"
        assert meta.exchange("UNKNOWN") == ""

    def test_product_type(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert meta.product_type("TXFC0") == "future"
        assert meta.product_type("2330") == "stock"
        assert meta.product_type("TXO22000C202604") == "option"

    def test_order_params(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        params = meta.order_params("TXFC0")
        assert isinstance(params, dict)

    def test_symbols_for_tags(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        result = meta.symbols_for_tags(["futures"])
        assert "TXFC0" in result

    def test_reload(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        meta.reload()  # should not raise
        assert meta.contains("TXFC0") if hasattr(meta, "contains") else "TXFC0" in meta.meta

    def test_reload_if_changed(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        changed = meta.reload_if_changed()
        assert isinstance(changed, bool)

    def test_meta_attribute(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert isinstance(meta.meta, dict)
        assert "TXFC0" in meta.meta

    def test_symbols_by_tag_attribute(self, symbols_yaml):
        meta = SymbolMetadata(symbols_yaml)
        assert isinstance(meta.symbols_by_tag, dict)
        # Writable (tests set this directly)
        meta.symbols_by_tag["test_tag"] = {"SYM1"}
        assert "SYM1" in meta.symbols_by_tag["test_tag"]

    def test_default_scale_class_constant(self, symbols_yaml):
        assert SymbolMetadata.DEFAULT_SCALE == 10_000

    def test_has_instrument_registry(self, symbols_yaml):
        """New: SymbolMetadata exposes its internal InstrumentRegistry."""
        meta = SymbolMetadata(symbols_yaml)
        assert hasattr(meta, "registry")
        from hft_platform.core.instrument_registry import InstrumentRegistry
        assert isinstance(meta.registry, InstrumentRegistry)
```

- [ ] **Step 2: Run tests to verify current state**

Run: `uv run pytest tests/unit/test_instrument_registry_compat.py -v`
Expected: Most pass (existing API works), but `test_has_instrument_registry` FAILS (no `.registry` attribute yet).

- [ ] **Step 3: Add InstrumentRegistry integration to SymbolMetadata**

Modify `src/hft_platform/feed_adapter/normalizer.py`. The changes are **additive** — all existing methods stay, we add registry population in `_load()` and expose `.registry`.

In `SymbolMetadata.__init__` (after line 145, after `self._load()`), add:

```python
        # --- InstrumentRegistry integration ---
        from hft_platform.core.instrument_registry import InstrumentRegistry
        self.registry: InstrumentRegistry = InstrumentRegistry(
            max_instruments=int(os.getenv("HFT_MAX_INSTRUMENTS", "5000")),
        )
        self._populate_registry()
```

Add new method `_populate_registry` (after `_load`):

```python
    def _populate_registry(self) -> None:
        """Build InstrumentProfile entries from symbols.yaml metadata."""
        from hft_platform.core.instrument_registry import (
            InstrumentProfile,
            InstrumentType,
            OptionRight,
            FeeStructure,
            TradingHours,
        )
        profiles = []
        for code, entry in self.meta.items():
            ptype_str = self.product_type(code)
            itype = {
                "future": InstrumentType.FUTURE,
                "option": InstrumentType.OPTION,
                "stock": InstrumentType.EQUITY,
                "equity": InstrumentType.EQUITY,
                "index": InstrumentType.INDEX,
            }.get(ptype_str, InstrumentType.EQUITY)

            # Fee structure defaults (overridable per-symbol in YAML)
            fee = FeeStructure(
                tax_rate_bps=int(entry.get("tax_rate_bps", 20)),
                commission_per_lot=int(entry.get("commission_per_lot", 130000)),
            )
            hours = TradingHours(
                day_open=str(entry.get("day_open", "08:45")),
                day_close=str(entry.get("day_close", "13:45")),
                night_open=entry.get("night_open"),
                night_close=entry.get("night_close"),
            )

            # Options-specific fields
            strike_scaled = None
            option_right = None
            expiry = None
            if itype == InstrumentType.OPTION:
                raw_strike = entry.get("strike") or entry.get("strike_price")
                if raw_strike is not None:
                    strike_scaled = int(float(raw_strike) * self.price_scale(code))
                raw_right = entry.get("right") or entry.get("option_right", "")
                if raw_right.upper() in ("C", "CALL"):
                    option_right = OptionRight.CALL
                elif raw_right.upper() in ("P", "PUT"):
                    option_right = OptionRight.PUT
                raw_expiry = entry.get("expiry")
                if raw_expiry is not None:
                    from datetime import date as _d
                    if isinstance(raw_expiry, _d):
                        expiry = raw_expiry
                    else:
                        try:
                            expiry = _d.fromisoformat(str(raw_expiry))
                        except ValueError:
                            pass

            profile = InstrumentProfile(
                symbol=code,
                instrument_type=itype,
                underlying=str(entry.get("underlying", "")),
                exchange=self.exchange(code),
                multiplier=self.contract_multiplier(code),
                tick_size_scaled=int(
                    float(entry.get("tick_size", 1.0)) * self.price_scale(code)
                ),
                price_scale=self.price_scale(code),
                fee_structure=fee,
                trading_hours=hours,
                lot_size=int(entry.get("lot_size", 1)),
                strike_scaled=strike_scaled,
                option_right=option_right,
                expiry=expiry,
            )
            profiles.append(profile)

        self.registry.reload_static(profiles)
```

In `reload()` method, add after `self._load()`:

```python
        self._populate_registry()
```

In `reload_if_changed()`, the existing code already calls `self.reload()` which now chains to `_populate_registry()`. No additional change needed.

- [ ] **Step 4: Run compat tests**

Run: `uv run pytest tests/unit/test_instrument_registry_compat.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Run ALL existing normalizer tests to verify no regression**

Run: `uv run pytest tests/unit/test_normalizer.py tests/unit/test_normalizer_error_paths.py tests/unit/test_normalizer_timestamp.py tests/unit/test_normalizer_deep.py tests/unit/test_market_data_normalizer_behavior.py -v --timeout=30`
Expected: All PASS, no regressions

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/feed_adapter/normalizer.py tests/unit/test_instrument_registry_compat.py
git commit -m "feat(normalizer): integrate InstrumentRegistry into SymbolMetadata with full backward compat"
```

---

### Task 4: ClickHouse Schema Migration

**Files:**
- Create: `src/hft_platform/migrations/clickhouse/20260330_001_add_instrument_columns.sql`

- [ ] **Step 1: Write the migration file**

```sql
-- 20260330_001_add_instrument_columns.sql
-- Multi-instrument support: add instrument_type, underlying, strike, option_right, expiry
-- to market_data, orders, and fills tables.
-- Deployment order: run this BEFORE deploying new recorder code.

-- Up

-- hft.market_data
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS underlying LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS strike_scaled Int64 DEFAULT 0;
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS option_right LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS expiry Date DEFAULT '1970-01-01';

-- hft.orders
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS oc_type LowCardinality(String) DEFAULT '';

-- hft.fills
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS oc_type LowCardinality(String) DEFAULT '';

-- Down

-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS instrument_type;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS underlying;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS strike_scaled;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS option_right;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS expiry;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS instrument_type;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS oc_type;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS instrument_type;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS oc_type;
```

- [ ] **Step 2: Verify migration follows existing patterns**

Run: `ls src/hft_platform/migrations/clickhouse/`
Confirm filename is alphabetically after `20260328_001_add_trade_direction.sql`.

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/migrations/clickhouse/20260330_001_add_instrument_columns.sql
git commit -m "chore(migrations): add instrument_type, underlying, strike, option_right, expiry columns"
```

---

### Task 5: Extend Recorder Column Lists (worker.py + _loader_batch.py)

**Files:**
- Modify: `src/hft_platform/recorder/worker.py:16-29` (MARKET_DATA_COLUMNS)
- Modify: `src/hft_platform/recorder/worker.py:72-107` (_extract_market_data_values)
- Modify: `src/hft_platform/recorder/_loader_batch.py:23-36` (_MARKET_DATA_COLS)
- Test: `tests/unit/test_recorder_worker.py` (existing), `tests/unit/test_recorder_loader.py` (existing)

**CRITICAL**: Both column lists must match exactly. Deployment requires ClickHouse migration first.

- [ ] **Step 1: Write failing tests for new columns**

Create `tests/unit/test_recorder_instrument_columns.py`:

```python
# tests/unit/test_recorder_instrument_columns.py
"""Verify recorder column lists include instrument metadata fields."""
from __future__ import annotations


def test_market_data_columns_include_instrument_fields():
    from hft_platform.recorder.worker import MARKET_DATA_COLUMNS
    required = ["instrument_type", "underlying", "strike_scaled", "option_right", "expiry"]
    for col in required:
        assert col in MARKET_DATA_COLUMNS, f"Missing column: {col}"


def test_loader_columns_match_worker_columns():
    from hft_platform.recorder.worker import MARKET_DATA_COLUMNS
    from hft_platform.recorder._loader_batch import _MARKET_DATA_COLS
    assert MARKET_DATA_COLUMNS == _MARKET_DATA_COLS, (
        f"Column mismatch!\n"
        f"  worker:  {MARKET_DATA_COLUMNS}\n"
        f"  loader:  {_MARKET_DATA_COLS}"
    )


def test_extract_market_data_values_length_matches_columns():
    from hft_platform.recorder.worker import (
        MARKET_DATA_COLUMNS,
        _extract_market_data_values,
    )
    row = {
        "symbol": "TXFC0",
        "exchange": "TAIFEX",
        "type": "Tick",
        "exch_ts": 1000000000,
        "ingest_ts": 1000000001,
        "price_scaled": 220000000,
        "volume": 1,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": 1,
        "instrument_type": "future",
        "underlying": "TX",
        "strike_scaled": 0,
        "option_right": "",
        "expiry": "1970-01-01",
    }
    values = _extract_market_data_values(row)
    assert values is not None
    assert len(values) == len(MARKET_DATA_COLUMNS), (
        f"Values length {len(values)} != columns length {len(MARKET_DATA_COLUMNS)}"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_recorder_instrument_columns.py -v`
Expected: FAIL — `instrument_type` not in MARKET_DATA_COLUMNS

- [ ] **Step 3: Extend MARKET_DATA_COLUMNS in worker.py**

In `src/hft_platform/recorder/worker.py`, change lines 16-29:

Replace the existing `MARKET_DATA_COLUMNS` list with:

```python
MARKET_DATA_COLUMNS = [
    "symbol", "exchange", "type",
    "exch_ts", "ingest_ts",
    "price_scaled", "volume",
    "bids_price", "bids_vol", "asks_price", "asks_vol",
    "seq_no",
    # Multi-instrument fields (added 2026-03-30)
    "instrument_type", "underlying", "strike_scaled", "option_right", "expiry",
]
```

- [ ] **Step 4: Extend _extract_market_data_values in worker.py**

In `_extract_market_data_values`, add 5 fields after `seq_no` extraction. At the end of the dict-path extraction (before `return`), add:

```python
        values.append(row.get("instrument_type", ""))
        values.append(row.get("underlying", ""))
        values.append(int(row.get("strike_scaled", 0)))
        values.append(row.get("option_right", ""))
        values.append(str(row.get("expiry", "1970-01-01")))
```

And in the object-path extraction, add the same 5 fields:

```python
        values.append(getattr(row, "instrument_type", ""))
        values.append(getattr(row, "underlying", ""))
        values.append(int(getattr(row, "strike_scaled", 0)))
        values.append(getattr(row, "option_right", ""))
        values.append(str(getattr(row, "expiry", "1970-01-01")))
```

- [ ] **Step 5: Extend _MARKET_DATA_COLS in _loader_batch.py**

In `src/hft_platform/recorder/_loader_batch.py`, change lines 23-36:

```python
_MARKET_DATA_COLS: list[str] = [
    "symbol", "exchange", "type",
    "exch_ts", "ingest_ts",
    "price_scaled", "volume",
    "bids_price", "bids_vol", "asks_price", "asks_vol",
    "seq_no",
    # Multi-instrument fields (added 2026-03-30)
    "instrument_type", "underlying", "strike_scaled", "option_right", "expiry",
]
```

Also update `format_market_data()` in the same file to extract these 5 fields from WAL row dicts. After the `seq_no` extraction, add:

```python
        row_data.append(r.get("instrument_type", ""))
        row_data.append(r.get("underlying", ""))
        row_data.append(int(r.get("strike_scaled", 0)))
        row_data.append(r.get("option_right", ""))
        row_data.append(str(r.get("expiry", "1970-01-01")))
```

- [ ] **Step 6: Run new tests**

Run: `uv run pytest tests/unit/test_recorder_instrument_columns.py -v`
Expected: All 3 tests PASS

- [ ] **Step 7: Run existing recorder tests for regression**

Run: `uv run pytest tests/unit/test_recorder_worker.py tests/unit/test_recorder_loader.py tests/unit/test_recorder_loader_edge.py tests/unit/test_recorder_mapper.py -v --timeout=30`
Expected: All PASS. If any fail due to hardcoded column count assertions, update those assertions to match new count (17).

- [ ] **Step 8: Commit**

```bash
git add src/hft_platform/recorder/worker.py src/hft_platform/recorder/_loader_batch.py tests/unit/test_recorder_instrument_columns.py
git commit -m "feat(recorder): extend column lists with instrument metadata (worker + loader in sync)"
```

---

### Task 6: Populate Instrument Metadata in Recorder Mapper

**Files:**
- Modify: `src/hft_platform/recorder/mapper.py:78-167`
- Test: `tests/unit/test_recorder_mapper.py` (existing)

The mapper must look up `InstrumentRegistry` (via `SymbolMetadata.registry`) to populate `instrument_type`, `underlying`, `strike_scaled`, `option_right`, `expiry` in every record dict.

- [ ] **Step 1: Write failing test for instrument fields in mapped records**

Create `tests/unit/test_recorder_mapper_instrument.py`:

```python
# tests/unit/test_recorder_mapper_instrument.py
"""Verify mapper populates instrument metadata fields."""
from __future__ import annotations

import tempfile
import yaml
import pytest

from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.recorder.mapper import map_event_to_record
from hft_platform.events import TickEvent, BidAskEvent
import numpy as np


@pytest.fixture
def metadata_with_registry(tmp_path):
    data = {
        "symbols": [
            {
                "code": "TXFC0",
                "exchange": "FUT",
                "tags": ["futures"],
                "point_value": 200,
                "tick_size": 1.0,
            },
        ],
    }
    path = tmp_path / "symbols.yaml"
    path.write_text(yaml.dump(data))
    return SymbolMetadata(str(path))


class TestMapperInstrumentFields:
    def test_tick_event_has_instrument_type(self, metadata_with_registry):
        tick = TickEvent(
            symbol="TXFC0", price=220000000, volume=1,
            ts=1000000000, meta=None,
            trade_direction=1, trade_confidence=1.0,
        )
        result = map_event_to_record(tick, metadata_with_registry)
        assert result is not None
        table, record = result
        assert record.get("instrument_type") == "future"
        assert record.get("underlying") == ""  # not set in yaml, defaults to ""

    def test_bidask_event_has_instrument_type(self, metadata_with_registry):
        ba = BidAskEvent(
            symbol="TXFC0",
            bids=np.array([[220000000, 5]], dtype=np.int64),
            asks=np.array([[220010000, 3]], dtype=np.int64),
            ts=1000000000, meta=None,
        )
        result = map_event_to_record(ba, metadata_with_registry)
        assert result is not None
        table, record = result
        assert record.get("instrument_type") == "future"

    def test_unknown_symbol_gets_empty_instrument_type(self, metadata_with_registry):
        tick = TickEvent(
            symbol="UNKNOWN", price=100, volume=1,
            ts=1000000000, meta=None,
            trade_direction=0, trade_confidence=0.0,
        )
        result = map_event_to_record(tick, metadata_with_registry)
        assert result is not None
        _, record = result
        assert record.get("instrument_type") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_recorder_mapper_instrument.py -v`
Expected: FAIL — `instrument_type` not in record dict

- [ ] **Step 3: Modify mapper to populate instrument fields**

In `src/hft_platform/recorder/mapper.py`, add a helper function near the top (after imports):

```python
def _instrument_fields(symbol: str, metadata: SymbolMetadata) -> dict[str, Any]:
    """Extract instrument metadata fields for ClickHouse record."""
    try:
        profile = metadata.registry.get(symbol)
        return {
            "instrument_type": profile.instrument_type.value,
            "underlying": profile.underlying,
            "strike_scaled": profile.strike_scaled or 0,
            "option_right": profile.option_right.value if profile.option_right else "",
            "expiry": str(profile.expiry) if profile.expiry else "1970-01-01",
        }
    except (KeyError, AttributeError):
        return {
            "instrument_type": "",
            "underlying": "",
            "strike_scaled": 0,
            "option_right": "",
            "expiry": "1970-01-01",
        }
```

Then in `map_event_to_record`, for both the `TickEvent` branch and the `BidAskEvent` branch, merge these fields into the returned dict. After building the record dict, add:

```python
        record.update(_instrument_fields(symbol, metadata))
```

Do this for both the TickEvent dict (around line 92) and BidAskEvent dict (around line 120).

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/unit/test_recorder_mapper_instrument.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run existing mapper tests for regression**

Run: `uv run pytest tests/unit/test_recorder_mapper.py -v --timeout=30`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/recorder/mapper.py tests/unit/test_recorder_mapper_instrument.py
git commit -m "feat(recorder): populate instrument metadata in mapped records via InstrumentRegistry"
```

---

### Task 7: Feature Registry vrr Cleanup

**Files:**
- Modify: `src/hft_platform/feature/registry.py:197-221`
- Test: `tests/unit/test_feature_engine.py` (existing — verify no regression)

Per spec and memory: `vrr_5_300_x1000` was computed in `engine.py` but NEVER registered in the registry. Toxicity took slot [21] in R23. The vrr computation code in engine.py is dead code.

- [ ] **Step 1: Verify vrr is not registered**

Run: `uv run python -c "from hft_platform.feature.registry import default_feature_registry; r = default_feature_registry(); fs = r.get_default(); print([f.feature_id for f in fs.features])"`
Expected: No `vrr_5_300_x1000` in the output list. `toxicity_ema50_x1000` at slot [21].

- [ ] **Step 2: Find and remove vrr dead code in engine.py**

Search for vrr computation code:

Run: `grep -n "vrr" src/hft_platform/feature/engine.py`

Remove any vrr-related computation blocks. This will be lines computing `vrr_5_300_x1000` that write to a feature array slot that doesn't exist in the registry.

- [ ] **Step 3: Run feature engine tests**

Run: `uv run pytest tests/unit/test_feature_engine.py tests/unit/test_feature_engine_v2.py tests/unit/test_feature_engine_v3_ema.py -v --timeout=30`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/feature/engine.py
git commit -m "refactor(feature): remove vrr dead code — never registered, slot taken by toxicity in R23"
```

---

### Task 8: Integration Verification

**Files:** None modified — verification only.

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/unit/ -x --timeout=60 -q`
Expected: All pass, zero regressions from Phase 1 changes.

- [ ] **Step 2: Run linter**

Run: `uv run ruff check src/hft_platform/core/instrument_registry.py src/hft_platform/feed_adapter/normalizer.py src/hft_platform/recorder/worker.py src/hft_platform/recorder/_loader_batch.py src/hft_platform/recorder/mapper.py src/hft_platform/feature/engine.py`
Expected: No errors

- [ ] **Step 3: Run type checker on new code**

Run: `uv run mypy src/hft_platform/core/instrument_registry.py --ignore-missing-imports`
Expected: No errors

- [ ] **Step 4: Verify InstrumentRegistry works end-to-end with real symbols.yaml**

Run: `uv run python -c "
from hft_platform.feed_adapter.normalizer import SymbolMetadata
m = SymbolMetadata()
print(f'Registry size: {m.registry.size}')
print(f'TMFD6 type: {m.registry.get(\"TMFD6\").instrument_type.value if m.registry.contains(\"TMFD6\") else \"not found\"}')
for sym in list(m.meta.keys())[:5]:
    if m.registry.contains(sym):
        p = m.registry.get(sym)
        print(f'  {sym}: type={p.instrument_type.value}, exchange={p.exchange}, multiplier={p.multiplier}')
"`
Expected: Registry populated with all symbols from `config/base/symbols.yaml`, correct types for futures vs stocks.

- [ ] **Step 5: Final commit with any fixes**

If any issues found in steps 1-4, fix and commit. Otherwise, Phase 1 is complete.
