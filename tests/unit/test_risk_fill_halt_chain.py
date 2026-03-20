"""Tests for the fill -> PnL -> DailyLossLimit -> order rejection chain.

Validates that realized PnL deltas propagate through RiskEngine.notify_fill_pnl()
to the DailyLossLimitValidator, and that the validator correctly rejects or
approves subsequent OrderIntents based on accumulated loss vs threshold.

All prices use scaled int (x10000) per platform convention.
"""

import asyncio

import pytest
import yaml

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.validators import DailyLossLimitValidator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path, *, max_daily_loss: int = 100_000_000, strategies: dict | None = None):
    """Write a minimal risk YAML config and return the path string."""
    cfg = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "max_notional": 10_000_000,
            "max_position_lots": 1000,
            "max_daily_loss": max_daily_loss,
        },
        "strategies": strategies or {},
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
    intent_type: IntentType = IntentType.NEW,
    strategy_id: str = "s1",
    symbol: str = "2330",
    side: Side = Side.BUY,
    price: int = 500_0000,  # 500 NTD in x10000
    qty: int = 1,
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
        timestamp_ns=timebase.now_ns(),
    )


def _get_daily_loss_validator(engine: RiskEngine) -> DailyLossLimitValidator:
    """Extract the DailyLossLimitValidator from the engine's validator chain."""
    for v in engine.validators:
        if isinstance(v, DailyLossLimitValidator):
            return v
    raise AssertionError("DailyLossLimitValidator not found in engine.validators")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    """RiskEngine with max_daily_loss = 100_000_000 (10,000 NTD x10000)."""
    cfg_path = _write_config(tmp_path, max_daily_loss=100_000_000)
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    return RiskEngine(cfg_path, q_in, q_out)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNotifyFillPnl:
    """Verify notify_fill_pnl forwards PnL deltas to the DailyLossLimitValidator."""

    def test_notify_fill_pnl_forwards_to_daily_loss_validator(self, engine):
        """A single negative PnL delta should appear in the validator's accumulated loss."""
        engine.notify_fill_pnl("s1", -50_000_000)

        validator = _get_daily_loss_validator(engine)
        assert validator._accumulated_loss["s1"] == -50_000_000

    def test_multiple_pnl_deltas_accumulate(self, engine):
        """Multiple PnL deltas should sum correctly."""
        engine.notify_fill_pnl("s1", -30_000_000)
        engine.notify_fill_pnl("s1", -20_000_000)

        validator = _get_daily_loss_validator(engine)
        assert validator._accumulated_loss["s1"] == -50_000_000

    def test_positive_pnl_offsets_losses(self, engine):
        """Gains should offset prior losses."""
        engine.notify_fill_pnl("s1", -80_000_000)
        engine.notify_fill_pnl("s1", 30_000_000)

        validator = _get_daily_loss_validator(engine)
        assert validator._accumulated_loss["s1"] == -50_000_000


class TestDailyLossLimitRejection:
    """Verify that accumulated loss triggers order rejection."""

    def test_loss_exceeds_threshold_rejects_order(self, engine):
        """When accumulated loss >= max_daily_loss, NEW intents must be rejected."""
        # Accumulate loss exactly at the threshold (100_000_000 = 10,000 NTD x10000)
        engine.notify_fill_pnl("s1", -100_000_000)

        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)

        assert decision.approved is False
        assert "DAILY_LOSS" in decision.reason_code

    def test_loss_beyond_threshold_rejects_order(self, engine):
        """When accumulated loss exceeds max_daily_loss, NEW intents must be rejected."""
        engine.notify_fill_pnl("s1", -150_000_000)

        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)

        assert decision.approved is False
        assert "DAILY_LOSS_LIMIT_EXCEEDED" in decision.reason_code

    def test_below_threshold_still_approved(self, engine):
        """When accumulated loss is below the limit, orders should pass."""
        # 99_999_999 is just under the 100_000_000 threshold
        engine.notify_fill_pnl("s1", -99_999_999)

        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)

        assert decision.approved is True

    def test_no_loss_is_approved(self, engine):
        """No PnL recorded at all -- order should be approved."""
        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)

        assert decision.approved is True

    def test_positive_pnl_is_approved(self, engine):
        """Net positive PnL should not trigger rejection."""
        engine.notify_fill_pnl("s1", 200_000_000)

        intent = _make_intent(strategy_id="s1")
        decision = engine.evaluate(intent)

        assert decision.approved is True

    def test_different_strategies_are_independent(self, engine):
        """Loss on strategy s1 should not affect s2."""
        engine.notify_fill_pnl("s1", -150_000_000)

        intent_s2 = _make_intent(strategy_id="s2")
        decision = engine.evaluate(intent_s2)

        assert decision.approved is True


class TestCancelBypassesDailyLossLimit:
    """CANCEL intents must always be allowed, even when daily loss limit is breached."""

    def test_cancel_intent_bypasses_daily_loss_limit(self, engine):
        """CANCEL intents should be approved even with loss over threshold."""
        engine.notify_fill_pnl("s1", -200_000_000)

        cancel_intent = _make_intent(
            strategy_id="s1",
            intent_type=IntentType.CANCEL,
        )
        decision = engine.evaluate(cancel_intent)

        assert decision.approved is True
        assert decision.reason_code == "OK"


class TestConfigReloadUpdatesThreshold:
    """Verify that config reload changes the daily loss limit threshold.

    DailyLossLimitValidator.check() resolves max_daily_loss via:
        strat_configs[strategy_id].get("max_daily_loss", _default_max_daily_loss)
    The reload path (on_config_reload) updates validator.strat_configs but does
    not re-run __init__, so per-strategy overrides are the correct reload vector.
    """

    def test_config_reload_updates_threshold(self, tmp_path):
        """After reloading with a per-strategy threshold, previously rejected orders pass."""
        # Start with strict global threshold: 50_000_000 (5,000 NTD x10000)
        cfg_path = _write_config(tmp_path, max_daily_loss=50_000_000)
        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        eng = RiskEngine(cfg_path, q_in, q_out)

        # Verify initial threshold
        validator = _get_daily_loss_validator(eng)
        assert validator._default_max_daily_loss == 50_000_000

        # Accumulate loss that exceeds initial threshold
        eng.notify_fill_pnl("s1", -60_000_000)
        intent = _make_intent(strategy_id="s1")
        decision = eng.evaluate(intent)
        assert decision.approved is False

        # Rewrite config: add per-strategy override with higher limit
        new_cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "tick_size": 0.01,
                "max_notional": 10_000_000,
                "max_position_lots": 1000,
                "max_daily_loss": 50_000_000,
            },
            "strategies": {
                "s1": {
                    "max_daily_loss": 200_000_000,
                },
            },
            "storm_guard": {
                "warm_threshold": -200_000,
                "storm_threshold": -500_000,
                "halt_threshold": -1_000_000,
            },
        }
        cfg_file = tmp_path / "risk.yaml"
        cfg_file.write_text(yaml.dump(new_cfg))

        # Reload config
        eng.reload_config()

        # Verify strat_configs updated on the validator
        validator = _get_daily_loss_validator(eng)
        assert validator.strat_configs["s1"]["max_daily_loss"] == 200_000_000

        # Same intent should now be approved (60M loss < 200M per-strategy threshold)
        decision = eng.evaluate(intent)
        assert decision.approved is True
