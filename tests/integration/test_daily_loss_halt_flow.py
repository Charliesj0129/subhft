"""Integration test: daily loss breach → HALT → cancel → Telegram.

Tests the full chain:
  DailyLossLimitValidator.halt_triggered → RiskEngine._check_daily_loss_halt()
  → StormGuard.state == HALT
  → NotificationDispatcher.notify_daily_loss / notify_halt called
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.validators import DailyLossLimitValidator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path, *, max_daily_loss: int = 100_000_000) -> str:
    """Write a minimal risk YAML config and return the file path string."""
    cfg = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "max_notional": 10_000_000_000,
            "per_symbol_max_notional": 10_000_000_000,
            "max_position_lots": 1000,
            "max_daily_loss": max_daily_loss,
        },
        "strategies": {},
        "storm_guard": {
            "warm_threshold": -200_000,
            "storm_threshold": -500_000,
            "halt_threshold": -1_000_000,
        },
    }
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.dump(cfg))
    return str(path)


def _make_intent(
    *,
    strategy_id: str = "s1",
    price: int = 500_0000,  # 500 NTD x10000
    qty: int = 1,
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        timestamp_ns=timebase.now_ns(),
    )


def _get_daily_loss_validator(engine: RiskEngine) -> DailyLossLimitValidator:
    for v in engine.validators:
        if isinstance(v, DailyLossLimitValidator):
            return v
    raise AssertionError("DailyLossLimitValidator not found")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    """RiskEngine with max_daily_loss = 100_000_000."""
    cfg_path = _write_config(tmp_path, max_daily_loss=100_000_000)
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    return RiskEngine(cfg_path, q_in, q_out)


@pytest.fixture
def engine_with_dispatcher(tmp_path):
    """RiskEngine wired with a mock NotificationDispatcher."""
    cfg_path = _write_config(tmp_path, max_daily_loss=100_000_000)
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    mock_dispatcher = MagicMock()
    mock_dispatcher.notify_daily_loss = AsyncMock()
    mock_dispatcher.notify_halt = AsyncMock()
    eng = RiskEngine(cfg_path, q_in, q_out, notification_dispatcher=mock_dispatcher)
    return eng, mock_dispatcher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDailyLossTriggersHaltState:
    """Verify that a daily loss breach escalates StormGuard to HALT."""

    def test_daily_loss_triggers_halt_state(self, engine):
        """Accumulated loss >= limit → evaluate() → StormGuard state == HALT."""
        # Precondition: StormGuard is NORMAL
        assert engine.storm_guard.state == StormGuardState.NORMAL

        # Accumulate loss at the threshold
        engine.notify_fill_pnl("s1", -100_000_000)

        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)

        # Order rejected due to daily loss
        assert decision.approved is False
        assert "DAILY_LOSS" in decision.reason_code

        # StormGuard must now be in HALT
        assert engine.storm_guard.state == StormGuardState.HALT

    def test_halt_state_blocks_subsequent_orders(self, engine):
        """After HALT, new orders from other strategies should also be blocked."""
        engine.notify_fill_pnl("s1", -150_000_000)
        engine.evaluate(_make_intent(strategy_id="s1"))

        # HALT should now block s2 orders too via StormGuard check
        assert engine.storm_guard.state == StormGuardState.HALT
        decision_s2 = engine.evaluate(_make_intent(strategy_id="s2"))
        assert decision_s2.approved is False
        assert "STORMGUARD_HALT" in decision_s2.reason_code

    def test_no_halt_when_below_threshold(self, engine):
        """Loss below limit must NOT trigger HALT."""
        engine.notify_fill_pnl("s1", -50_000_000)
        engine.evaluate(_make_intent(strategy_id="s1"))

        assert engine.storm_guard.state == StormGuardState.NORMAL

    def test_halt_triggered_flag_set_on_validator(self, engine):
        """Verify DailyLossLimitValidator.halt_triggered is set before engine checks it."""
        engine.notify_fill_pnl("s1", -100_000_000)
        engine.evaluate(_make_intent(strategy_id="s1"))

        validator = _get_daily_loss_validator(engine)
        assert validator.halt_triggered is True

    def test_update_unrealized_pnl_contributes_to_halt(self, engine):
        """Unrealized loss via update_unrealized_pnl should also trigger HALT."""
        # No realized loss, but unrealized loss exceeds limit
        engine.update_unrealized_pnl(-120_000_000)

        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)

        assert decision.approved is False
        assert engine.storm_guard.state == StormGuardState.HALT


class TestDailyLossSendsTelegramNotification:
    """Verify NotificationDispatcher.notify_daily_loss and notify_halt are called."""

    @pytest.mark.asyncio
    async def test_daily_loss_sends_telegram_notification(self, engine_with_dispatcher):
        """After loss breach + evaluate(), both notification methods are scheduled."""
        engine, mock_dispatcher = engine_with_dispatcher

        engine.notify_fill_pnl("s1", -100_000_000)

        intent = _make_intent(strategy_id="s1")
        engine.evaluate(intent)

        # Give the event loop a tick to execute created tasks
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # two yields to flush task queue

        mock_dispatcher.notify_daily_loss.assert_called_once()
        mock_dispatcher.notify_halt.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_notification_without_dispatcher(self, engine):
        """Engine without dispatcher must not raise when HALT is triggered."""
        engine.notify_fill_pnl("s1", -100_000_000)
        intent = _make_intent(strategy_id="s1")
        # Must not raise
        decision = engine.evaluate(intent)
        assert decision.approved is False
        assert engine.storm_guard.state == StormGuardState.HALT

    @pytest.mark.asyncio
    async def test_notification_called_once_on_repeated_evaluate(self, engine_with_dispatcher):
        """Once HALT is set, repeated evaluate() calls must NOT re-send notification."""
        engine, mock_dispatcher = engine_with_dispatcher

        engine.notify_fill_pnl("s1", -100_000_000)

        # First evaluate — triggers HALT + notification
        engine.evaluate(_make_intent(strategy_id="s1"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        call_count_after_first = mock_dispatcher.notify_daily_loss.call_count

        # Second evaluate — already HALT, should not re-trigger
        engine.evaluate(_make_intent(strategy_id="s2"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert mock_dispatcher.notify_daily_loss.call_count == call_count_after_first


class TestUnrealizedPnlTriggersHaltWithoutEvaluate:
    """Verify update_unrealized_pnl triggers HALT without waiting for evaluate()."""

    def test_unrealized_loss_triggers_halt_via_update(self, engine):
        """Unrealized loss exceeding limit triggers HALT without evaluate()."""
        assert engine.storm_guard.state == StormGuardState.NORMAL

        # Record some realized loss near threshold
        engine.notify_fill_pnl("s1", -80_000_000)

        # Unrealized loss pushes total over the limit — should trigger HALT
        # without needing a new intent/evaluate() call.
        engine.update_unrealized_pnl(-30_000_000)

        assert engine.storm_guard.state == StormGuardState.HALT
