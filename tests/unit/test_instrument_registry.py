"""Tests for InstrumentProfile and related data structures in instrument_registry."""
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
