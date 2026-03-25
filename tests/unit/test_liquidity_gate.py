"""Tests for LiquidityGateValidator."""
import pytest
from unittest.mock import MagicMock
from hft_platform.risk.liquidity_gate import LiquidityGateValidator
from hft_platform.contracts.strategy import OrderIntent, Side, IntentType, TIF


def _make_intent(intent_type=IntentType.NEW):
    return OrderIntent(
        intent_id=1, strategy_id="TEST", symbol="TMFD6",
        intent_type=intent_type, side=Side.BUY, price=200000000, qty=1,
    )


def _make_lob(spread_scaled=10000):
    lob = MagicMock()
    stats = MagicMock()
    stats.spread_scaled = spread_scaled
    lob.last_stats = stats
    return lob


def _make_validator(lob=None, reject_ticks=3, tick_size_scaled=10000):
    cfg = {
        "liquidity_gate": {
            "spread_reject_ticks": reject_ticks,
            "spread_warn_ticks": 2,
            "cooldown_s": 5,
            "gate_start_offset_s": 60,
        },
    }
    v = LiquidityGateValidator(cfg, None, lob=lob, tick_size_scaled=tick_size_scaled)
    v._gate_active = True  # bypass time-of-day offset for testing
    return v


class TestLiquidityGate:
    def test_allows_normal_spread(self):
        lob = _make_lob(spread_scaled=10000)  # 1 tick
        v = _make_validator(lob=lob)
        ok, reason = v.check(_make_intent())
        assert ok is True

    def test_rejects_wide_spread(self):
        lob = _make_lob(spread_scaled=40000)  # 4 ticks > 3 threshold
        v = _make_validator(lob=lob)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SPREAD_TOO_WIDE" in reason

    def test_allows_cancel_during_wide_spread(self):
        lob = _make_lob(spread_scaled=40000)
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent(intent_type=IntentType.CANCEL))
        assert ok is True

    def test_allows_force_flat_during_wide_spread(self):
        lob = _make_lob(spread_scaled=40000)
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent(intent_type=IntentType.FORCE_FLAT))
        assert ok is True

    def test_allows_when_no_lob_data(self):
        lob = MagicMock()
        lob.last_stats = None
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent())
        assert ok is True

    def test_exact_threshold_allowed(self):
        lob = _make_lob(spread_scaled=30000)  # exactly 3 ticks
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent())
        assert ok is True  # reject only when strictly greater
