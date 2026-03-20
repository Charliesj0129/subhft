"""Comprehensive tests for risk validators in hft_platform.risk.validators."""

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.risk.validators import (
    DailyLossLimitValidator,
    PerSymbolNotionalValidator,
    PositionLimitValidator,
    PriceBandValidator,
    StormGuardFSM,
)

_DEFAULT_CONFIG = {
    "global_defaults": {
        "max_price_cap": 5000.0,
        "tick_size": 0.01,
        "price_band_ticks": 20,
        "max_notional": 10_000_000,
        "max_position_lots": 1000,
        "max_daily_loss": 500_000_000,
        "per_symbol_max_notional": 50_000_000,
    },
    "strategies": {},
}


def _make_provider() -> MagicMock:
    provider = MagicMock()
    provider.price_scale.return_value = 10000
    return provider


def make_intent(
    price: int = 5_000_000,
    qty: int = 10,
    symbol: str = "2330",
    strategy_id: str = "s1",
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
    )


@pytest.fixture(autouse=True)
def mock_metrics():
    with patch("hft_platform.risk.validators.MetricsRegistry") as m:
        registry = MagicMock()
        m.get.return_value = registry
        yield registry


class TestPriceBandValidator:
    def _make_validator(self, lob=None):
        return PriceBandValidator(
            config=_DEFAULT_CONFIG,
            price_scale_provider=_make_provider(),
            lob=lob,
        )

    def test_cancel_always_passes(self):
        v = self._make_validator()
        intent = make_intent(price=0, intent_type=IntentType.CANCEL)
        ok, reason = v.check(intent)
        assert ok is True
        assert reason == "OK"

    @pytest.mark.parametrize("price", [0, -100])
    def test_zero_or_negative_price_rejected(self, price):
        v = self._make_validator()
        intent = make_intent(price=price)
        ok, reason = v.check(intent)
        assert ok is False
        assert reason == "PRICE_ZERO_OR_NEG"

    def test_price_exceeds_cap_rejected(self):
        v = self._make_validator()
        intent = make_intent(price=50_000_001)
        ok, reason = v.check(intent)
        assert ok is False
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_lob_band_within_passes(self):
        lob = MagicMock()
        lob.get_l1_scaled.return_value = (0, 0, 0, 10_000_000)
        v = self._make_validator(lob=lob)
        intent = make_intent(price=5_001_000)
        ok, reason = v.check(intent)
        assert ok is True
        assert reason == "OK"

    def test_lob_band_outside_rejected(self):
        lob = MagicMock()
        lob.get_l1_scaled.return_value = (0, 0, 0, 10_000_000)
        v = self._make_validator(lob=lob)
        intent = make_intent(price=5_010_000)
        ok, reason = v.check(intent)
        assert ok is False
        assert "PRICE_OUTSIDE_BAND" in reason


class TestDailyLossLimitValidator:
    def _make_validator(self):
        return DailyLossLimitValidator(
            config=_DEFAULT_CONFIG,
            price_scale_provider=_make_provider(),
        )

    def test_no_loss_passes(self):
        v = self._make_validator()
        ok, reason = v.check(make_intent())
        assert ok is True

    def test_loss_below_limit_passes(self):
        v = self._make_validator()
        v.record_pnl("s1", -100_000_000)
        ok, reason = v.check(make_intent())
        assert ok is True

    def test_loss_at_limit_rejected(self):
        v = self._make_validator()
        v.record_pnl("s1", -500_000_000)
        ok, reason = v.check(make_intent())
        assert ok is False
        assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason

    def test_record_pnl_accumulates(self):
        v = self._make_validator()
        v.record_pnl("s1", -200_000_000)
        v.record_pnl("s1", -300_000_000)
        ok, reason = v.check(make_intent())
        assert ok is False
        assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason

    def test_day_boundary_reset(self):
        v = self._make_validator()
        v.record_pnl("s1", -500_000_000)
        ok, _ = v.check(make_intent())
        assert ok is False
        ns_per_day = 86_400 * 1_000_000_000
        v._current_date_ns -= ns_per_day
        ok, reason = v.check(make_intent())
        assert ok is True
        assert reason == "OK"


class TestPerSymbolNotionalValidator:
    def _make_validator(self, max_cache=10000):
        v = PerSymbolNotionalValidator(
            config=_DEFAULT_CONFIG,
            price_scale_provider=_make_provider(),
        )
        v._MAX_CACHE_ENTRIES = max_cache
        return v

    def test_normal_order_passes(self):
        v = self._make_validator()
        intent = make_intent(price=5_000_000, qty=10)
        ok, reason = v.check(intent)
        assert ok is True

    def test_notional_exceeds_limit_rejected(self):
        v = self._make_validator()
        intent = make_intent(price=50_000_000, qty=100_000)
        ok, reason = v.check(intent)
        assert ok is False
        assert "PER_SYMBOL_NOTIONAL_EXCEEDED" in reason

    def test_cache_overflow_clears(self):
        v = self._make_validator(max_cache=2)
        v.check(make_intent(symbol="AAA", strategy_id="s1"))
        v.check(make_intent(symbol="BBB", strategy_id="s1"))
        assert len(v._per_symbol_notional_cache) == 2
        v.check(make_intent(symbol="CCC", strategy_id="s1"))
        assert len(v._per_symbol_notional_cache) == 1
        assert ("s1", "CCC") in v._per_symbol_notional_cache

    def test_cancel_always_passes(self):
        v = self._make_validator()
        intent = make_intent(intent_type=IntentType.CANCEL)
        ok, reason = v.check(intent)
        assert ok is True
        assert reason == "OK"


class TestPositionLimitValidator:
    def _make_validator(self, config=None):
        return PositionLimitValidator(
            config=config or _DEFAULT_CONFIG,
            price_scale_provider=_make_provider(),
        )

    def test_normal_qty_passes(self):
        v = self._make_validator()
        ok, reason = v.check(make_intent(qty=500))
        assert ok is True

    def test_qty_exceeds_limit_rejected(self):
        v = self._make_validator()
        ok, reason = v.check(make_intent(qty=1001))
        assert ok is False
        assert "POSITION_LIMIT_EXCEEDED" in reason

    def test_per_strategy_override(self):
        config = {
            **_DEFAULT_CONFIG,
            "strategies": {"s1": {"max_position_lots": 5}},
        }
        v = self._make_validator(config=config)
        ok, _ = v.check(make_intent(qty=5))
        assert ok is True
        ok, reason = v.check(make_intent(qty=6))
        assert ok is False
        assert "POSITION_LIMIT_EXCEEDED" in reason


class TestStormGuardFSM:
    _SG_CONFIG = {
        "storm_guard": {
            "warm_threshold": -200_000,
            "storm_threshold": -500_000,
            "halt_threshold": -1_000_000,
        },
    }

    def _make_fsm(self):
        return StormGuardFSM(self._SG_CONFIG)

    def test_escalation_sequence(self):
        fsm = self._make_fsm()
        assert fsm.state == StormGuardState.NORMAL
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

    def test_halt_allows_cancel_blocks_new(self):
        fsm = self._make_fsm()
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT
        ok, reason = fsm.validate(make_intent(intent_type=IntentType.CANCEL))
        assert ok is True
        assert reason == "OK"
        ok, reason = fsm.validate(make_intent(intent_type=IntentType.NEW))
        assert ok is False
        assert reason == "STORMGUARD_HALT"

    def test_de_escalation_requires_consecutive_clears(self):
        fsm = self._make_fsm()
        fsm._de_escalate_threshold = 3
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL
