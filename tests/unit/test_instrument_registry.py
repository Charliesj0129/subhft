"""Tests for InstrumentProfile and related data structures in instrument_registry."""
from __future__ import annotations

from datetime import date

import pytest

from hft_platform.core.instrument_registry import (
    FeeStructure,
    InstrumentProfile,
    InstrumentType,
    OptionRight,
    TradingHours,
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
        hours = TradingHours(
            day_open="08:45", day_close="13:45",
            night_open=None, night_close=None,
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


# ---------------------------------------------------------------------------
# InstrumentRegistry tests
# ---------------------------------------------------------------------------

from hft_platform.core.instrument_registry import InstrumentLimitError, InstrumentRegistry  # noqa: E402


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
        trading_hours=TradingHours(
            day_open="08:45", day_close="13:45", night_open=None, night_close=None
        ),
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
        trading_hours=TradingHours(
            day_open="08:45", day_close="13:45", night_open=None, night_close=None
        ),
        strike_scaled=strike,
        option_right=right,
        expiry=expiry,
    )


class TestInstrumentRegistry:
    def test_register_and_get(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        profile = _make_future()
        reg.register(profile, source="static")
        assert reg.get("TXFC0") is profile

    def test_get_missing_raises_keyerror(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        with pytest.raises(KeyError):
            reg.get("NONEXISTENT")

    def test_contains(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(_make_future(), source="static")
        assert reg.contains("TXFC0")
        assert not reg.contains("NONEXISTENT")

    def test_get_by_underlying(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(_make_future("TXFC0", "TX"), source="static")
        reg.register(_make_future("TXFC1", "TX"), source="static")
        reg.register(_make_future("MXFC0", "MTX"), source="static")
        result = reg.get_by_underlying("TX")
        assert {p.symbol for p in result} == {"TXFC0", "TXFC1"}

    def test_get_options_chain(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        exp = date(2026, 4, 15)
        reg.register(
            _make_option("TXO22000C202604", "TX", 220000000, OptionRight.CALL, exp),
            source="dynamic",
        )
        reg.register(
            _make_option("TXO22500P202604", "TX", 225000000, OptionRight.PUT, exp),
            source="dynamic",
        )
        reg.register(
            _make_option(
                "TXO22000C202605", "TX", 220000000, OptionRight.CALL, date(2026, 5, 21)
            ),
            source="dynamic",
        )
        chain = reg.get_options_chain("TX", exp)
        assert len(chain) == 2
        assert all(p.expiry == exp for p in chain)

    def test_bulk_register(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        profiles = [_make_future(f"TXF{i}", "TX") for i in range(5)]
        reg.bulk_register(profiles, source="static")
        assert len(reg.get_by_underlying("TX")) == 5

    def test_evict_expired(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(
            _make_option("EXPIRED1", "TX", 220000000, OptionRight.CALL, date(2026, 3, 1)),
            source="dynamic",
        )
        reg.register(
            _make_option("ACTIVE1", "TX", 220000000, OptionRight.CALL, date(2026, 5, 1)),
            source="dynamic",
        )
        evicted = reg.evict_expired(as_of=date(2026, 4, 1))
        assert evicted == 1
        assert not reg.contains("EXPIRED1")
        assert reg.contains("ACTIVE1")

    def test_cardinality_guard_rejects_over_limit(self) -> None:
        reg = InstrumentRegistry(max_instruments=3)
        for i in range(3):
            reg.register(_make_future(f"F{i}", "TX"), source="static")
        with pytest.raises(InstrumentLimitError):
            reg.register(_make_future("F3", "TX"), source="static")

    def test_cardinality_guard_evicts_expired_first(self) -> None:
        reg = InstrumentRegistry(max_instruments=3)
        reg.register(
            _make_option("EXP1", "TX", 220000000, OptionRight.CALL, date(2026, 1, 1)),
            source="dynamic",
        )
        reg.register(_make_future("F1", "TX"), source="static")
        reg.register(_make_future("F2", "TX"), source="static")
        # At capacity (3); this should evict EXP1 (expired relative to today 2026-03-29)
        reg.register(_make_future("F3", "TX"), source="static")
        assert not reg.contains("EXP1")
        assert reg.contains("F3")

    def test_static_reload_preserves_dynamic(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(_make_future("TXFC0", "TX"), source="static")
        reg.register(
            _make_option(
                "TXO22000C202604", "TX", 220000000, OptionRight.CALL, date(2026, 4, 15)
            ),
            source="dynamic",
        )
        reg.reload_static([_make_future("TXFC0_NEW", "TX")])
        assert not reg.contains("TXFC0")
        assert reg.contains("TXFC0_NEW")
        assert reg.contains("TXO22000C202604")

    def test_update_existing_symbol(self) -> None:
        """Re-registering an existing symbol should update, not raise."""
        reg = InstrumentRegistry(max_instruments=100)
        reg.register(_make_future("TXFC0", "TX"), source="static")
        new_profile = _make_future("TXFC0", "TX")
        reg.register(new_profile, source="static")
        assert reg.get("TXFC0") is new_profile
        assert reg.size == 1

    def test_size_property(self) -> None:
        reg = InstrumentRegistry(max_instruments=100)
        assert reg.size == 0
        reg.register(_make_future(), source="static")
        assert reg.size == 1
