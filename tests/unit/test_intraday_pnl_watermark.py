"""Tests for DailyLossLimitValidator intraday watermark extensions."""

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.validators import DailyLossLimitValidator


def _make_intent(strategy_id="TEST", symbol="TMFD6", side=Side.BUY, price=200000000, qty=1):
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=side,
        price=price,
        qty=qty,
    )


def _make_validator(config=None):
    """Create validator with intraday_pnl config.

    Unit conversion: TMF price_scale=10000, point_value=10.
    1 NTD = price_scale / point_value = 1000 scaled units.
    So: 500 NTD = 500_000, 1000 NTD = 1_000_000 in scaled-int.
    """
    defaults = {
        "max_daily_loss": 1_000_000,
    }
    intraday_pnl = {
        "soft_limit_ntd": 500,
        "hard_limit_ntd": 1000,
        "peak_drawdown_pct": 0.40,
        "soft_recovery_ntd": 300,
        "drawdown_recovery_pct": 0.20,
        "soft_limit_cooldown_s": 60,
        "peak_drawdown_min_peak_ntd": 200,
        "price_scale": 10000,
        "point_value": 10,
    }
    cfg = config or {}
    cfg.setdefault("global_defaults", defaults)
    cfg.setdefault("intraday_pnl", intraday_pnl)
    v = DailyLossLimitValidator(cfg, None)
    return v


class TestSoftLimit:
    def test_allows_order_above_soft_limit(self):
        v = _make_validator()
        v.record_pnl("TEST", -400_000)  # -400 NTD, above -500 soft limit
        ok, reason = v.check(_make_intent())
        assert ok is True

    def test_soft_limit_triggers_reduce_only_flag(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)  # -550 NTD, below -500 soft limit
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SOFT_LIMIT" in reason
        assert v.soft_limit_active is True

    def test_soft_limit_allows_cancel(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        cancel_intent = _make_intent()
        cancel_intent.intent_type = IntentType.CANCEL
        ok, reason = v.check(cancel_intent)
        assert ok is True

    def test_soft_limit_allows_force_flat(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        flat_intent = _make_intent()
        flat_intent.intent_type = IntentType.FORCE_FLAT
        ok, reason = v.check(flat_intent)
        assert ok is True

    def test_soft_limit_recovery_blocked_by_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())  # triggers soft limit
        assert v.soft_limit_active is True
        v.record_pnl("TEST", 350_000)  # accumulated now = -200_000 (-200 NTD)
        ok, _ = v.check(_make_intent())
        assert ok is False
        assert v.soft_limit_active is True

    def test_soft_limit_recovery_after_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())  # triggers soft limit
        v.record_pnl("TEST", 350_000)  # accumulated = -200_000
        v._soft_limit_cooldown_until_ns = 0  # force cooldown expired
        ok, _ = v.check(_make_intent())
        assert ok is True
        assert v.soft_limit_active is False

    def test_oscillation_resets_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())
        v.record_pnl("TEST", 350_000)
        v._soft_limit_cooldown_until_ns = 0
        v.check(_make_intent())  # recovers
        assert v.soft_limit_active is False
        v.record_pnl("TEST", -400_000)  # now at -600_000
        v.check(_make_intent())  # re-triggers
        assert v.soft_limit_active is True
        assert v._soft_limit_cooldown_until_ns > 0


class TestPeakDrawdown:
    def test_peak_drawdown_ignored_when_peak_below_minimum(self):
        v = _make_validator()
        v.record_pnl("TEST", 100_000)  # +100 NTD
        v.check(_make_intent())  # updates peak
        v.record_pnl("TEST", -150_000)  # total = -50_000
        ok, _ = v.check(_make_intent())
        assert ok is True  # peak < 200 NTD minimum

    def test_peak_drawdown_triggers_when_peak_above_minimum(self):
        v = _make_validator()
        v.record_pnl("TEST", 300_000)  # +300 NTD
        v.check(_make_intent())  # peak = 300_000
        v.record_pnl("TEST", -150_000)  # total = 150_000, drawdown = 150_000 > 120_000
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "PEAK_DRAWDOWN" in reason

    def test_peak_drawdown_allows_when_drawdown_small(self):
        v = _make_validator()
        v.record_pnl("TEST", 300_000)
        v.check(_make_intent())
        v.record_pnl("TEST", -50_000)  # drawdown = 50_000 < 120_000
        ok, _ = v.check(_make_intent())
        assert ok is True


class TestHardLimit:
    def test_hard_limit_triggers_halt(self):
        v = _make_validator()
        v.record_pnl("TEST", -1_050_000)  # -1050 NTD
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert v.halt_triggered is True

    def test_hard_limit_not_recoverable(self):
        v = _make_validator()
        v.record_pnl("TEST", -1_050_000)
        v.check(_make_intent())
        v.record_pnl("TEST", 1_050_000)  # back to 0
        ok, _ = v.check(_make_intent())
        assert ok is False
        assert v.halt_triggered is True


class TestReset:
    def test_daily_reset_clears_watermark_state(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())
        assert v.soft_limit_active is True
        v._force_reset()
        assert v.soft_limit_active is False
        assert v._peak_pnl_scaled == 0
