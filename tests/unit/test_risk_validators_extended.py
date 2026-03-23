"""WU-16: Exhaustive parametrized tests for all risk validators."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side, StormGuardState
from hft_platform.core.pricing import FixedPriceScaleProvider
from hft_platform.risk.validators import (
    DailyLossLimitValidator,
    MaxNotionalValidator,
    PositionLimitValidator,
    PriceBandValidator,
    StormGuardFSM,
)

SCALE = 10_000  # default price scale factor


def _intent(
    price: int = 10_000,
    qty: int = 1,
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
    strategy_id: str = "strat_a",
    symbol: str = "2330",
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        target_order_id=None,
        timestamp_ns=0,
    )


def _cfg(**overrides: Any) -> dict:
    base: dict = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": 10_000_000,
            "max_position_lots": 1_000,
            "max_daily_loss": 500_000_000,
        },
        "strategies": {},
    }
    base.update(overrides)
    return base


def _provider() -> FixedPriceScaleProvider:
    return FixedPriceScaleProvider(scale=SCALE)


# ---------------------------------------------------------------------------
# PriceBandValidator
# ---------------------------------------------------------------------------


class TestPriceBandValidator:
    @pytest.mark.parametrize(
        "price, expected_ok",
        [
            (0, False),
            (-1, False),
            (-100_000, False),
        ],
        ids=["zero", "negative_one", "large_negative"],
    )
    def test_zero_or_negative_price(self, price: int, expected_ok: bool) -> None:
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider())
        ok, reason = v.check(_intent(price=price))
        assert ok is expected_ok
        assert "PRICE_ZERO_OR_NEG" in reason

    def test_price_exactly_at_cap(self) -> None:
        """Price exactly at cap boundary should pass."""
        cap = 5000.0
        v = PriceBandValidator(
            _cfg(global_defaults={**_cfg()["global_defaults"], "max_price_cap": cap}),
            price_scale_provider=_provider(),
        )
        ok, _ = v.check(_intent(price=int(cap * SCALE)))
        assert ok is True

    def test_price_one_tick_above_cap(self) -> None:
        cap = 5000.0
        v = PriceBandValidator(
            _cfg(global_defaults={**_cfg()["global_defaults"], "max_price_cap": cap}),
            price_scale_provider=_provider(),
        )
        ok, reason = v.check(_intent(price=int(cap * SCALE) + 1))
        assert ok is False
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_price_well_within_cap(self) -> None:
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider())
        ok, reason = v.check(_intent(price=100 * SCALE))
        assert ok is True
        assert reason == "OK"

    def test_cancel_bypasses_all_checks(self) -> None:
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider())
        ok, reason = v.check(_intent(price=0, intent_type=IntentType.CANCEL))
        assert ok is True
        assert reason == "OK"

    def test_cancel_bypasses_cap_check(self) -> None:
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider())
        ok, _ = v.check(_intent(price=999_999_999, intent_type=IntentType.CANCEL))
        assert ok is True

    # LOB-relative band tests
    def test_lob_band_within(self) -> None:
        lob = MagicMock()
        lob.get_l1_scaled.return_value = None
        lob.get_book_snapshot.return_value = {"mid_price": 100 * SCALE}
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=lob)
        ok, _ = v.check(_intent(price=100 * SCALE))
        assert ok is True

    def test_lob_band_outside_high(self) -> None:
        mid = 100 * SCALE
        lob = MagicMock()
        lob.get_l1_scaled.return_value = None
        lob.get_book_snapshot.return_value = {"mid_price": mid}
        # band_ticks=20, tick_size=0.01 => band_width = 20 * 100 = 2000
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=lob)
        ok, reason = v.check(_intent(price=mid + 2001))
        assert ok is False
        assert "PRICE_OUTSIDE_BAND" in reason

    def test_lob_band_outside_low(self) -> None:
        mid = 100 * SCALE
        lob = MagicMock()
        lob.get_l1_scaled.return_value = None
        lob.get_book_snapshot.return_value = {"mid_price": mid}
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=lob)
        ok, reason = v.check(_intent(price=mid - 2001))
        assert ok is False
        assert "PRICE_OUTSIDE_BAND" in reason

    def test_lob_band_exactly_at_boundary(self) -> None:
        mid = 100 * SCALE
        lob = MagicMock()
        lob.get_l1_scaled.return_value = None
        lob.get_book_snapshot.return_value = {"mid_price": mid}
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=lob)
        # Exactly at upper bound should pass (not strict >)
        ok, _ = v.check(_intent(price=mid + 2000))
        assert ok is True

    def test_no_lob_skips_band_check(self) -> None:
        """Without LOB, only absolute cap is checked."""
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=None)
        ok, _ = v.check(_intent(price=4999 * SCALE))
        assert ok is True

    def test_lob_zero_mid_price_skips_band(self) -> None:
        lob = MagicMock()
        lob.get_l1_scaled.return_value = None
        lob.get_book_snapshot.return_value = {"mid_price": 0}
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=lob)
        ok, _ = v.check(_intent(price=100 * SCALE))
        assert ok is True

    def test_lob_exception_skips_band(self) -> None:
        lob = MagicMock()
        lob.get_l1_scaled.side_effect = RuntimeError("boom")
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=lob)
        ok, _ = v.check(_intent(price=100 * SCALE))
        assert ok is True

    def test_lob_l1_scaled_path(self) -> None:
        """When get_l1_scaled returns valid data, use that instead of book snapshot."""
        mid = 200 * SCALE
        lob = MagicMock()
        lob.get_l1_scaled.return_value = (0, 0, 0, mid * 2)  # mid_price_x2
        v = PriceBandValidator(_cfg(), price_scale_provider=_provider(), lob=lob)
        ok, _ = v.check(_intent(price=mid))
        assert ok is True

    def test_strategy_specific_band_ticks(self) -> None:
        mid = 100 * SCALE
        lob = MagicMock()
        lob.get_l1_scaled.return_value = None
        lob.get_book_snapshot.return_value = {"mid_price": mid}
        cfg = _cfg(strategies={"strat_a": {"price_band_ticks": 5}})
        v = PriceBandValidator(cfg, price_scale_provider=_provider(), lob=lob)
        # band = 5 * 100 = 500
        ok, _ = v.check(_intent(price=mid + 501))
        assert ok is False


# ---------------------------------------------------------------------------
# MaxNotionalValidator
# ---------------------------------------------------------------------------


class TestMaxNotionalValidator:
    def test_within_limit(self) -> None:
        v = MaxNotionalValidator(_cfg(), price_scale_provider=_provider())
        ok, reason = v.check(_intent(price=100 * SCALE, qty=5))
        assert ok is True
        assert reason == "OK"

    def test_exceeds_limit(self) -> None:
        cfg = _cfg(global_defaults={**_cfg()["global_defaults"], "max_notional": 100})
        v = MaxNotionalValidator(cfg, price_scale_provider=_provider())
        # notional = 100*10000 * 5 = 5_000_000, limit = 100 * 10000 = 1_000_000
        ok, reason = v.check(_intent(price=100 * SCALE, qty=5))
        assert ok is False
        assert "MAX_NOTIONAL_EXCEEDED" in reason

    def test_per_strategy_override(self) -> None:
        cfg = _cfg(
            global_defaults={**_cfg()["global_defaults"], "max_notional": 10_000_000},
            strategies={"strat_a": {"max_notional": 50}},
        )
        v = MaxNotionalValidator(cfg, price_scale_provider=_provider())
        # strat_a limit = 50 * 10000 = 500_000. price*qty = 100*10000 * 1 = 1_000_000
        ok, reason = v.check(_intent(price=100 * SCALE, qty=1))
        assert ok is False
        assert "MAX_NOTIONAL_EXCEEDED" in reason

    def test_zero_qty(self) -> None:
        v = MaxNotionalValidator(_cfg(), price_scale_provider=_provider())
        ok, _ = v.check(_intent(price=100 * SCALE, qty=0))
        assert ok is True

    def test_cancel_bypass(self) -> None:
        cfg = _cfg(global_defaults={**_cfg()["global_defaults"], "max_notional": 1})
        v = MaxNotionalValidator(cfg, price_scale_provider=_provider())
        ok, _ = v.check(_intent(price=999_999, qty=999, intent_type=IntentType.CANCEL))
        assert ok is True

    def test_exactly_at_limit(self) -> None:
        # limit = 100 * 10000 = 1_000_000. notional = 10*10000 * 10 = 1_000_000
        cfg = _cfg(global_defaults={**_cfg()["global_defaults"], "max_notional": 100})
        v = MaxNotionalValidator(cfg, price_scale_provider=_provider())
        ok, _ = v.check(_intent(price=10 * SCALE, qty=10))
        assert ok is True

    def test_amend_still_checked(self) -> None:
        cfg = _cfg(global_defaults={**_cfg()["global_defaults"], "max_notional": 1})
        v = MaxNotionalValidator(cfg, price_scale_provider=_provider())
        ok, reason = v.check(_intent(price=100 * SCALE, qty=1, intent_type=IntentType.AMEND))
        assert ok is False
        assert "MAX_NOTIONAL_EXCEEDED" in reason


# ---------------------------------------------------------------------------
# PositionLimitValidator
# ---------------------------------------------------------------------------


class TestPositionLimitValidator:
    def test_within_limit(self) -> None:
        v = PositionLimitValidator(_cfg(), price_scale_provider=_provider())
        ok, _ = v.check(_intent(qty=500))
        assert ok is True

    def test_exceeds_limit(self) -> None:
        v = PositionLimitValidator(_cfg(), price_scale_provider=_provider())
        ok, reason = v.check(_intent(qty=1001))
        assert ok is False
        assert "POSITION_LIMIT_EXCEEDED" in reason

    def test_negative_qty_abs(self) -> None:
        """Uses abs(qty), so negative qty exceeding limit should fail."""
        v = PositionLimitValidator(_cfg(), price_scale_provider=_provider())
        ok, reason = v.check(_intent(qty=-1001))
        assert ok is False
        assert "POSITION_LIMIT_EXCEEDED" in reason

    def test_per_strategy_override(self) -> None:
        cfg = _cfg(strategies={"strat_a": {"max_position_lots": 10}})
        v = PositionLimitValidator(cfg, price_scale_provider=_provider())
        ok, reason = v.check(_intent(qty=11))
        assert ok is False
        assert "POSITION_LIMIT_EXCEEDED" in reason

    def test_cancel_bypass(self) -> None:
        v = PositionLimitValidator(_cfg(), price_scale_provider=_provider())
        ok, _ = v.check(_intent(qty=99999, intent_type=IntentType.CANCEL))
        assert ok is True

    def test_exactly_at_limit(self) -> None:
        v = PositionLimitValidator(_cfg(), price_scale_provider=_provider())
        ok, _ = v.check(_intent(qty=1000))
        assert ok is True


# ---------------------------------------------------------------------------
# StormGuardFSM (validators module)
# ---------------------------------------------------------------------------


class TestStormGuardFSM:
    def _fsm(self, **overrides: Any) -> StormGuardFSM:
        cfg: dict = {
            "storm_guard": {
                "warm_threshold": -200_000,
                "storm_threshold": -500_000,
                "halt_threshold": -1_000_000,
            }
        }
        cfg["storm_guard"].update(overrides)
        return StormGuardFSM(cfg)

    def test_initial_state_normal(self) -> None:
        fsm = self._fsm()
        assert fsm.state == StormGuardState.NORMAL

    @pytest.mark.parametrize(
        "pnl, expected_state",
        [
            (0, StormGuardState.NORMAL),
            (-100_000, StormGuardState.NORMAL),
            (-200_000, StormGuardState.WARM),
            (-200_001, StormGuardState.WARM),
            (-500_000, StormGuardState.STORM),
            (-500_001, StormGuardState.STORM),
            (-1_000_000, StormGuardState.HALT),
            (-1_500_000, StormGuardState.HALT),
        ],
        ids=[
            "zero_normal",
            "slight_loss_normal",
            "warm_boundary",
            "warm_past",
            "storm_boundary",
            "storm_past",
            "halt_boundary",
            "halt_past",
        ],
    )
    def test_escalation_from_normal(self, pnl: int, expected_state: StormGuardState) -> None:
        fsm = self._fsm()
        fsm.update_pnl(pnl)
        assert fsm.state == expected_state

    def test_escalation_chain_normal_to_halt(self) -> None:
        fsm = self._fsm()
        fsm.update_pnl(-200_000)
        assert fsm.state == StormGuardState.WARM
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM
        fsm.update_pnl(-1_000_000)
        assert fsm.state == StormGuardState.HALT

    def test_validate_halt_blocks_new(self) -> None:
        fsm = self._fsm()
        fsm.update_pnl(-1_000_000)
        ok, reason = fsm.validate(_intent())
        assert ok is False
        assert reason == "STORMGUARD_HALT"

    def test_validate_halt_allows_cancel(self) -> None:
        fsm = self._fsm()
        fsm.update_pnl(-1_000_000)
        ok, _ = fsm.validate(_intent(intent_type=IntentType.CANCEL))
        assert ok is True

    def test_validate_storm_blocks_new(self) -> None:
        fsm = self._fsm()
        fsm.update_pnl(-500_000)
        ok, reason = fsm.validate(_intent())
        assert ok is False
        assert "STORMGUARD_STORM_NEW_BLOCKED" in reason

    def test_validate_storm_allows_amend(self) -> None:
        fsm = self._fsm()
        fsm.update_pnl(-500_000)
        ok, _ = fsm.validate(_intent(intent_type=IntentType.AMEND))
        assert ok is True

    def test_validate_warm_allows_new(self) -> None:
        fsm = self._fsm()
        fsm.update_pnl(-200_000)
        ok, _ = fsm.validate(_intent())
        assert ok is True

    def test_validate_normal_allows_all(self) -> None:
        fsm = self._fsm()
        ok, _ = fsm.validate(_intent())
        assert ok is True

    def test_shim_update_pnl_escalation_and_recovery(self) -> None:
        """StormGuardFSM shim: update_pnl maps PnL to state (no hysteresis in shim).

        Hysteresis is now in StormGuard.update() — tested in test_stormguard_*.py.
        """
        fsm = self._fsm()
        fsm.update_pnl(-500_000)
        assert fsm.state == StormGuardState.STORM

        # Shim sets state directly (no hysteresis)
        fsm.update_pnl(0)
        assert fsm.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# DailyLossLimitValidator
# ---------------------------------------------------------------------------


class TestDailyLossLimitValidator:
    def test_no_loss_passes(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        ok, _ = v.check(_intent())
        assert ok is True

    def test_below_limit_passes(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        v.record_pnl("strat_a", -100_000_000)
        ok, _ = v.check(_intent())
        assert ok is True

    def test_at_limit_rejects(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        v.record_pnl("strat_a", -500_000_000)
        ok, reason = v.check(_intent())
        assert ok is False
        assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason

    def test_exceeds_limit_rejects(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        v.record_pnl("strat_a", -600_000_000)
        ok, reason = v.check(_intent())
        assert ok is False
        assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason

    def test_cancel_bypass(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        v.record_pnl("strat_a", -999_999_999_999)
        ok, _ = v.check(_intent(intent_type=IntentType.CANCEL))
        assert ok is True

    def test_net_positive_passes(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        v.record_pnl("strat_a", -300_000_000)
        v.record_pnl("strat_a", 400_000_000)  # net +100M
        ok, _ = v.check(_intent())
        assert ok is True

    def test_per_strategy_isolation(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        v.record_pnl("strat_a", -600_000_000)
        # strat_b has no losses
        ok, _ = v.check(_intent(strategy_id="strat_b"))
        assert ok is True

    def test_per_strategy_limit_override(self) -> None:
        cfg = _cfg(strategies={"strat_a": {"max_daily_loss": 100_000}})
        v = DailyLossLimitValidator(cfg, price_scale_provider=_provider())
        v.record_pnl("strat_a", -100_000)
        ok, reason = v.check(_intent())
        assert ok is False
        assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason

    def test_zero_drawdown_passes(self) -> None:
        v = DailyLossLimitValidator(_cfg(), price_scale_provider=_provider())
        v.record_pnl("strat_a", 0)
        ok, _ = v.check(_intent())
        assert ok is True
