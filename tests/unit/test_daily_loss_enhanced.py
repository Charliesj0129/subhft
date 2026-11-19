"""Tests for enhanced DailyLossLimitValidator (unrealized PnL + 05:00 reset + halt flag)."""

from __future__ import annotations

from unittest.mock import patch

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.validators import DailyLossLimitValidator

_LIMIT = 100_000_000
_REALIZED_5K_LOSS = -50_000_000
_REALIZED_8K_LOSS = -80_000_000
_UNREALIZED_6K_LOSS = -60_000_000
_UNREALIZED_3K_GAIN = 30_000_000


def _make_validator(max_daily_loss: int = _LIMIT) -> DailyLossLimitValidator:
    config = {"global_defaults": {"max_daily_loss": max_daily_loss}, "strategies": {}}
    return DailyLossLimitValidator(config)


def _make_intent(strategy_id: str = "s1") -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol="TMF",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=200_0000,
        qty=1,
    )


def test_unrealized_pnl_included_in_loss_check() -> None:
    """realized -5k + unrealized -6k = -11k > limit -10k → reject."""
    v = _make_validator()
    v.record_pnl("s1", _REALIZED_5K_LOSS)
    v.update_unrealized(_UNREALIZED_6K_LOSS)
    approved, reason = v.check(_make_intent())
    assert not approved
    assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason


def test_realized_only_below_limit_passes() -> None:
    """realized -5k + unrealized 0 → total -5k < -10k limit → pass."""
    v = _make_validator()
    v.record_pnl("s1", _REALIZED_5K_LOSS)
    v.update_unrealized(0)
    approved, reason = v.check(_make_intent())
    assert approved
    assert reason == "OK"


def test_unrealized_profit_offsets_realized_loss() -> None:
    """realized -8k + unrealized +3k = -5k → still below 10k limit → pass."""
    v = _make_validator()
    v.record_pnl("s1", _REALIZED_8K_LOSS)
    v.update_unrealized(_UNREALIZED_3K_GAIN)
    approved, reason = v.check(_make_intent())
    assert approved
    assert reason == "OK"


def test_halt_triggered_flag_set_on_breach() -> None:
    """After a breach, validator.halt_triggered must be True."""
    v = _make_validator()
    assert v.halt_triggered is False
    v.record_pnl("s1", _REALIZED_8K_LOSS)
    v.update_unrealized(_UNREALIZED_6K_LOSS)
    v.check(_make_intent())
    assert v.halt_triggered is True


def test_reset_clears_accumulated_loss() -> None:
    """After _force_reset(), validator no longer rejects on prior loss."""
    v = _make_validator()
    v.record_pnl("s1", _REALIZED_8K_LOSS)
    v.update_unrealized(_UNREALIZED_6K_LOSS)
    approved_before, _ = v.check(_make_intent())
    assert not approved_before

    v._force_reset()

    approved_after, reason = v.check(_make_intent())
    assert approved_after
    assert reason == "OK"
    assert v.halt_triggered is False
    assert v._unrealized_pnl == 0


def test_cancel_always_passes_even_when_breached() -> None:
    v = _make_validator()
    v.record_pnl("s1", _REALIZED_8K_LOSS)
    v.update_unrealized(_UNREALIZED_6K_LOSS)
    cancel_intent = OrderIntent(
        intent_id=2,
        strategy_id="s1",
        symbol="TMF",
        intent_type=IntentType.CANCEL,
        side=Side.BUY,
        price=200_0000,
        qty=1,
    )
    approved, reason = v.check(cancel_intent)
    assert approved
    assert reason == "OK"


def test_reset_occurs_at_0500_taiwan_time() -> None:
    """_maybe_reset() fires when clock passes 21:00 UTC (05:00 Taiwan)."""
    _21h_ns = 21 * 3600 * 1_000_000_000

    before_ns = _21h_ns - 1_000_000_000

    with patch("hft_platform.core.timebase.now_ns", return_value=before_ns):
        v = _make_validator()

    with patch("hft_platform.core.timebase.now_ns", return_value=before_ns):
        v.record_pnl("s1", _REALIZED_5K_LOSS)
    assert "s1" in v._accumulated_loss

    with patch("hft_platform.core.timebase.now_ns", return_value=before_ns):
        v.record_pnl("s1", 0)
        assert v._accumulated_loss.get("s1", 0) == _REALIZED_5K_LOSS

    after_ns = _21h_ns + 1_000_000_000

    with patch("hft_platform.core.timebase.now_ns", return_value=after_ns):
        v.record_pnl("s1", 0)
        assert v._accumulated_loss.get("s1", 0) == 0
