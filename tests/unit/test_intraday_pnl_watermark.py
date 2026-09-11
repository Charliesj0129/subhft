"""Tests for DailyLossLimitValidator intraday watermark extensions."""

from types import SimpleNamespace

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


def _ntd(amount_ntd: int) -> int:
    """NTD -> accumulator units.

    The PnL accumulator is fed by PositionStore / MtMCalculator, which have
    already multiplied by the contract multiplier (the point value), so one NTD
    is exactly ``price_scale`` units -- 10,000 -- for every instrument.

    These tests used to write the amounts as ``NTD * 1000``, mirroring the
    ``price_scale // point_value`` conversion the validator used to apply. That
    conversion divided by the point value a second time and enforced every
    threshold 10x too tight in production (2026-08-21: a 5,000 NTD hard limit
    halted at 670 NTD). Going through this helper keeps the unit stated once.
    """
    return amount_ntd * 10_000


def _make_validator(config=None):
    """Create validator with intraday_pnl config.

    Thresholds below are in NTD; use ``_ntd()`` for accumulator amounts.
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
        v.record_pnl("TEST", _ntd(-400))  # -400 NTD, above the -500 soft limit
        ok, reason = v.check(_make_intent())
        assert ok is True

    def test_soft_limit_triggers_reduce_only_flag(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-550))  # -550 NTD, past the -500 soft limit
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SOFT_LIMIT" in reason
        assert v.soft_limit_active is True

    def test_soft_limit_allows_cancel(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-550))
        cancel_intent = _make_intent()
        cancel_intent.intent_type = IntentType.CANCEL
        ok, reason = v.check(cancel_intent)
        assert ok is True

    def test_soft_limit_allows_force_flat(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-550))
        flat_intent = _make_intent()
        flat_intent.intent_type = IntentType.FORCE_FLAT
        ok, reason = v.check(flat_intent)
        assert ok is True

    def test_soft_limit_recovery_blocked_by_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-550))
        v.check(_make_intent())  # triggers soft limit
        assert v.soft_limit_active is True
        v.record_pnl("TEST", _ntd(350))  # accumulated now = -200 NTD
        ok, _ = v.check(_make_intent())
        assert ok is False
        assert v.soft_limit_active is True

    def test_soft_limit_recovery_after_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-550))
        v.check(_make_intent())  # triggers soft limit
        v.record_pnl("TEST", _ntd(350))  # accumulated = -200 NTD
        v._soft_limit_cooldown_until_ns = 0  # force cooldown expired
        ok, _ = v.check(_make_intent())
        assert ok is True
        assert v.soft_limit_active is False

    def test_oscillation_resets_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-550))
        v.check(_make_intent())
        v.record_pnl("TEST", _ntd(350))
        v._soft_limit_cooldown_until_ns = 0
        v.check(_make_intent())  # recovers
        assert v.soft_limit_active is False
        v.record_pnl("TEST", _ntd(-400))  # now at -600 NTD
        v.check(_make_intent())  # re-triggers
        assert v.soft_limit_active is True
        assert v._soft_limit_cooldown_until_ns > 0

    def test_soft_limit_allows_flat_strategy_after_cooldown_without_pnl_recovery(self):
        """Bug #39: flat strategies must not deadlock forever under SOFT_LIMIT."""
        position_state = SimpleNamespace(net=0)

        def _provider(symbol, strategy_id):
            return position_state.net

        v = DailyLossLimitValidator(_make_validator().config, None, position_provider=_provider)
        v.record_pnl("TEST", _ntd(-550))

        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SOFT_LIMIT" in reason
        assert v.soft_limit_active is True

        v._soft_limit_cooldown_until_ns = 0
        ok, reason = v.check(_make_intent())
        assert ok is True
        assert reason == "SOFT_LIMIT_FLAT_COOLDOWN_BYPASS"
        assert v.soft_limit_active is True

    def test_soft_limit_flat_cooldown_bypass_does_not_allow_readding_after_fill(self):
        """Cooldown escape is only for flat state; once exposed, SOFT_LIMIT still binds."""
        position_state = SimpleNamespace(net=0)

        def _provider(symbol, strategy_id):
            return position_state.net

        v = DailyLossLimitValidator(_make_validator().config, None, position_provider=_provider)
        v.record_pnl("TEST", _ntd(-550))
        v.check(_make_intent())  # trigger soft limit

        v._soft_limit_cooldown_until_ns = 0
        ok, reason = v.check(_make_intent())
        assert ok is True
        assert reason == "SOFT_LIMIT_FLAT_COOLDOWN_BYPASS"

        position_state.net = 1
        ok, reason = v.check(_make_intent(side=Side.BUY))
        assert ok is False
        assert "SOFT_LIMIT" in reason


class TestPeakDrawdown:
    def test_peak_drawdown_ignored_when_peak_below_minimum(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(100))  # +100 NTD
        v.check(_make_intent())  # updates peak
        v.record_pnl("TEST", _ntd(-150))  # total = -50 NTD
        ok, _ = v.check(_make_intent())
        assert ok is True  # peak < 200 NTD minimum

    def test_peak_drawdown_triggers_when_peak_above_minimum(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(300))  # +300 NTD
        v.check(_make_intent())  # peak = +300 NTD
        v.record_pnl("TEST", _ntd(-150))  # total = +150 NTD, drawdown = 150 > 120 (40% of 300)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "PEAK_DRAWDOWN" in reason

    def test_peak_drawdown_allows_when_drawdown_small(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(300))
        v.check(_make_intent())
        v.record_pnl("TEST", _ntd(-50))  # drawdown = 50 NTD < 120 NTD
        ok, _ = v.check(_make_intent())
        assert ok is True

    def test_peak_drawdown_sets_halt_triggered_for_force_flatten(self):
        """PEAK_DRAWDOWN MUST set halt_triggered so risk/engine.py escalates
        StormGuard to HALT, which triggers autonomy_monitor.flatten_all() —
        closes all positions at market per user 'lock-in profits when peak
        retracement breached' policy (2026-04-20)."""
        v = _make_validator()
        v.record_pnl("TEST", _ntd(300))  # peak = +300 NTD
        v.check(_make_intent())
        v.record_pnl("TEST", _ntd(-150))  # drawdown = 150 > 120 (40% of 300)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "PEAK_DRAWDOWN" in reason
        assert v.halt_triggered is True


class TestHardLimit:
    def test_hard_limit_triggers_halt(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-1050))  # -1050 NTD, past the -1000 hard limit
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert v.halt_triggered is True

    def test_hard_limit_not_recoverable(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-1050))
        v.check(_make_intent())
        v.record_pnl("TEST", _ntd(1050))  # back to 0
        ok, _ = v.check(_make_intent())
        assert ok is False
        assert v.halt_triggered is True


class TestReset:
    def test_daily_reset_clears_watermark_state(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-550))
        v.check(_make_intent())
        assert v.soft_limit_active is True
        v._force_reset()
        assert v.soft_limit_active is False
        assert v._peak_pnl_scaled == 0


def _make_wide_soft_limit_validator():
    """Validator whose soft limit is out of the way, isolating the hard limit."""
    return _make_validator(
        {
            "intraday_pnl": {
                "soft_limit_ntd": 5000,
                "hard_limit_ntd": 1000,
                "peak_drawdown_pct": 0.40,
                "soft_recovery_ntd": 300,
                "drawdown_recovery_pct": 0.20,
                "soft_limit_cooldown_s": 60,
                "peak_drawdown_min_peak_ntd": 200,
                "price_scale": 10000,
                "point_value": 10,
            }
        }
    )


class TestStaleUnrealizedDoubleCount:
    """``_accumulated_loss`` is fed at the fill; ``_unrealized_pnl`` is fed by a
    1 Hz mark-to-market tick. Between the two, the closing fill's profit is
    present as realized AND as the not-yet-cleared mark of the position it just
    closed, so a naive sum counts the same money twice.

    Production 2026-09-11T00:45:41Z: a book that went +795 -> +1045 -> +2320
    with no retracement at all recorded a peak of +4270 and halted for 20 hours
    on a reported 45.7% drawdown. The reported drawdown, 1950, was exactly the
    stale unrealized value.
    """

    def test_peak_ignores_stale_unrealized_after_realizing_fill(self):
        v = _make_validator()
        # Open position marked at +1950 NTD, consistent sample.
        v.update_unrealized(_ntd(1950))
        ok, _ = v.check(_make_intent())
        assert ok is True
        assert v._peak_pnl_scaled == _ntd(1950)

        # The closing fill books +2320 realized. The mark has NOT been
        # refreshed yet, so _unrealized_pnl still carries +1950 for a position
        # that no longer exists.
        v.record_pnl("TEST", _ntd(2320))
        ok, _ = v.check(_make_intent())
        assert ok is True
        # The peak must not absorb the double count.
        assert v._peak_pnl_scaled == _ntd(1950)

        # MtM catches up: flat, so unrealized is zero and the true total is
        # +2320 -- a new high, not a drawdown.
        v.update_unrealized(0)
        ok, reason = v.check(_make_intent())
        assert ok is True, reason
        assert v.halt_triggered is False
        assert v._peak_pnl_scaled == _ntd(2320)

    def test_hard_limit_not_tripped_by_double_counted_loss(self):
        # Same staleness, opposite sign: the hard limit must measure the real
        # loss, not the transient double count.
        v = _make_wide_soft_limit_validator()
        v.update_unrealized(_ntd(-800))  # real loss 800 NTD, cap is 1000
        ok, _ = v.check(_make_intent())
        assert ok is True
        assert v.halt_triggered is False

        v.record_pnl("TEST", _ntd(-800))  # closed at the marked loss
        ok, reason = v.check(_make_intent())
        assert ok is True, reason
        assert v.halt_triggered is False

        v.update_unrealized(0)
        ok, reason = v.check(_make_intent())
        assert ok is True, reason
        assert v.halt_triggered is False

    def test_daily_reset_clears_stale_marker(self):
        v = _make_validator()
        v.update_unrealized(_ntd(1950))
        v.record_pnl("TEST", _ntd(2320))
        assert v._unrealized_stale is True
        v._force_reset()
        assert v._unrealized_stale is False
        assert v._last_consistent_total_pnl == 0


class TestPeakDrawdownRecovery:
    """``drawdown_recovery_pct`` is configured in prod and base YAML and was
    read nowhere. A peak-drawdown halt latched until the 21:00 UTC daily reset.
    """

    def _halted(self):
        v = _make_validator()
        v.record_pnl("TEST", _ntd(300))
        v.check(_make_intent())  # peak = +300 NTD
        v.record_pnl("TEST", _ntd(-150))  # drawdown 150 > 120 (40% of 300)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "PEAK_DRAWDOWN" in reason
        assert v.halt_triggered is True
        assert v._halt_reason == "PEAK_DRAWDOWN"
        return v

    def test_peak_drawdown_releases_after_recovery(self):
        v = self._halted()
        v.record_pnl("TEST", _ntd(100))  # total = +250, drawdown 50 <= 60 (20%)
        v._peak_drawdown_halt_ts_ns = 0  # force cooldown expired
        v.update_unrealized(0, complete=True)
        assert v.halt_triggered is False
        assert v._halt_reason == ""
        ok, reason = v.check(_make_intent())
        assert ok is True, reason

    def test_peak_drawdown_release_requires_cooldown(self):
        v = self._halted()
        v.record_pnl("TEST", _ntd(100))  # recovered, but cooldown still running
        v.update_unrealized(0, complete=True)
        assert v.halt_triggered is True
        assert v._halt_reason == "PEAK_DRAWDOWN"

    def test_peak_drawdown_release_requires_recovery(self):
        v = self._halted()
        v.record_pnl("TEST", _ntd(20))  # total = +170, drawdown 130 > 60
        v._peak_drawdown_halt_ts_ns = 0
        v.update_unrealized(0, complete=True)
        assert v.halt_triggered is True

    def test_hard_limit_halt_does_not_release_on_recovery(self):
        # The hard daily-loss halt keeps its designed sticky semantics.
        v = _make_validator()
        v.record_pnl("TEST", _ntd(-1050))
        v.check(_make_intent())
        assert v.halt_triggered is True
        assert v._halt_reason == "DAILY_LOSS_LIMIT"
        v.record_pnl("TEST", _ntd(1050))  # back to flat PnL
        v._peak_drawdown_halt_ts_ns = 0
        v.update_unrealized(0, complete=True)
        assert v.halt_triggered is True

    def test_peak_drawdown_release_requires_complete_snapshot(self):
        # A partial valuation may latch a stop but never lift one.
        v = self._halted()
        v.record_pnl("TEST", _ntd(100))
        v._peak_drawdown_halt_ts_ns = 0
        v.update_unrealized(0, complete=False)
        assert v.halt_triggered is True
