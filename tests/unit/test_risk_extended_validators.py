"""Unit tests for PositionLimitValidator and DailyLossLimitValidator.

Tests follow the pattern established in the risk validator codebase:
- Use minimal config dicts
- Build OrderIntent directly with scaled-int prices (x10000)
- No external dependencies (no LOB, no broker)
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.risk.validators import DailyLossLimitValidator, PositionLimitValidator


def _make_intent(
    qty: int = 10,
    price: int = 1_000_000,  # 100.0000 scaled x10000
    strategy_id: str = "strat_a",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


def _make_config(
    max_position_lots: int = 100,
    max_daily_loss: int = 500_000_000,
    strategies: dict | None = None,
) -> dict:
    cfg: dict = {
        "global_defaults": {
            "max_position_lots": max_position_lots,
            "max_daily_loss": max_daily_loss,
        },
        "strategies": strategies or {},
    }
    return cfg


# ---------------------------------------------------------------------------
# PositionLimitValidator tests
# ---------------------------------------------------------------------------


class TestPositionLimitValidator(unittest.TestCase):
    def _make_validator(self, max_lots: int = 100, strategies: dict | None = None) -> PositionLimitValidator:
        config = _make_config(max_position_lots=max_lots, strategies=strategies)
        return PositionLimitValidator(config)

    def test_allows_order_within_limit(self) -> None:
        validator = self._make_validator(max_lots=100)
        intent = _make_intent(qty=50)
        ok, reason = validator.check(intent)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_allows_order_at_exact_limit(self) -> None:
        validator = self._make_validator(max_lots=100)
        intent = _make_intent(qty=100)
        ok, reason = validator.check(intent)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_rejects_order_exceeding_limit(self) -> None:
        validator = self._make_validator(max_lots=100)
        intent = _make_intent(qty=101)
        ok, reason = validator.check(intent)
        self.assertFalse(ok)
        self.assertIn("POSITION_LIMIT_EXCEEDED", reason)

    def test_rejects_large_qty(self) -> None:
        validator = self._make_validator(max_lots=10)
        intent = _make_intent(qty=999)
        ok, reason = validator.check(intent)
        self.assertFalse(ok)
        self.assertIn("POSITION_LIMIT_EXCEEDED", reason)

    def test_cancel_always_passes(self) -> None:
        """CANCEL intents must bypass position limit check."""
        validator = self._make_validator(max_lots=1)
        intent = _make_intent(qty=10_000, intent_type=IntentType.CANCEL)
        ok, reason = validator.check(intent)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_per_strategy_override(self) -> None:
        """Per-strategy max_position_lots overrides the global default."""
        strategies = {"strat_a": {"max_position_lots": 5}}
        validator = self._make_validator(max_lots=100, strategies=strategies)

        # strat_a is limited to 5
        intent_a = _make_intent(qty=6, strategy_id="strat_a")
        ok, reason = validator.check(intent_a)
        self.assertFalse(ok)
        self.assertIn("POSITION_LIMIT_EXCEEDED", reason)

        # strat_b falls back to global default (100)
        intent_b = _make_intent(qty=50, strategy_id="strat_b")
        ok, reason = validator.check(intent_b)
        self.assertTrue(ok)

    def test_abs_qty_used_for_sell_side(self) -> None:
        """Negative qty (sell) should use abs value for comparison."""
        validator = self._make_validator(max_lots=50)
        # Sell intent with qty=60 exceeds limit of 50
        intent = _make_intent(qty=60, intent_type=IntentType.NEW)
        intent = OrderIntent(
            intent_id=2,
            strategy_id="strat_a",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.SELL,
            price=1_000_000,
            qty=60,
        )
        ok, reason = validator.check(intent)
        self.assertFalse(ok)
        self.assertIn("POSITION_LIMIT_EXCEEDED", reason)

    def test_cache_is_populated(self) -> None:
        """Max position cache should be populated after first check."""
        validator = self._make_validator(max_lots=100)
        intent = _make_intent(qty=10, strategy_id="strat_x")
        validator.check(intent)
        self.assertIn("strat_x", validator._max_position_cache)
        self.assertEqual(validator._max_position_cache["strat_x"], 100)


# ---------------------------------------------------------------------------
# DailyLossLimitValidator tests
# ---------------------------------------------------------------------------


class TestDailyLossLimitValidator(unittest.TestCase):
    def _make_validator(
        self, max_daily_loss: int = 500_000_000, strategies: dict | None = None
    ) -> DailyLossLimitValidator:
        config = _make_config(max_daily_loss=max_daily_loss, strategies=strategies)
        return DailyLossLimitValidator(config)

    def test_allows_order_when_no_loss_recorded(self) -> None:
        validator = self._make_validator()
        intent = _make_intent()
        ok, reason = validator.check(intent)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_allows_order_when_loss_below_limit(self) -> None:
        validator = self._make_validator(max_daily_loss=500_000_000)
        validator.record_pnl("strat_a", -100_000_000)  # loss of 100M < limit 500M
        intent = _make_intent()
        ok, reason = validator.check(intent)
        self.assertTrue(ok)

    def test_rejects_order_when_loss_at_limit(self) -> None:
        validator = self._make_validator(max_daily_loss=500_000_000)
        validator.record_pnl("strat_a", -500_000_000)  # exactly at limit
        intent = _make_intent()
        ok, reason = validator.check(intent)
        self.assertFalse(ok)
        self.assertIn("DAILY_LOSS_LIMIT_EXCEEDED", reason)

    def test_rejects_order_when_loss_exceeds_limit(self) -> None:
        validator = self._make_validator(max_daily_loss=500_000_000)
        validator.record_pnl("strat_a", -600_000_000)  # 600M > 500M limit
        intent = _make_intent()
        ok, reason = validator.check(intent)
        self.assertFalse(ok)
        self.assertIn("DAILY_LOSS_LIMIT_EXCEEDED", reason)

    def test_tracks_losses_per_strategy(self) -> None:
        """Losses must be tracked independently per strategy."""
        validator = self._make_validator(max_daily_loss=500_000_000)
        # strat_a hits limit, strat_b does not
        validator.record_pnl("strat_a", -600_000_000)
        validator.record_pnl("strat_b", -100_000_000)

        intent_a = _make_intent(strategy_id="strat_a")
        ok_a, reason_a = validator.check(intent_a)
        self.assertFalse(ok_a)
        self.assertIn("DAILY_LOSS_LIMIT_EXCEEDED", reason_a)

        intent_b = _make_intent(strategy_id="strat_b")
        ok_b, reason_b = validator.check(intent_b)
        self.assertTrue(ok_b)
        self.assertEqual(reason_b, "OK")

    def test_cumulative_pnl_accumulates(self) -> None:
        """Multiple record_pnl calls must accumulate correctly."""
        validator = self._make_validator(max_daily_loss=500_000_000)
        validator.record_pnl("strat_a", -200_000_000)
        validator.record_pnl("strat_a", -200_000_000)
        # Total = -400M, below 500M limit
        intent = _make_intent()
        ok, _ = validator.check(intent)
        self.assertTrue(ok)

        validator.record_pnl("strat_a", -200_000_000)
        # Total = -600M, exceeds 500M limit
        ok, reason = validator.check(intent)
        self.assertFalse(ok)
        self.assertIn("DAILY_LOSS_LIMIT_EXCEEDED", reason)

    def test_gain_offsets_loss(self) -> None:
        """Gains recorded after losses should reduce effective daily loss."""
        validator = self._make_validator(max_daily_loss=500_000_000)
        validator.record_pnl("strat_a", -600_000_000)  # exceeds limit
        validator.record_pnl("strat_a", 200_000_000)  # gain of 200M
        # Net = -400M, below 500M limit — should now pass
        intent = _make_intent()
        ok, reason = validator.check(intent)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_cancel_always_passes(self) -> None:
        """CANCEL intents must bypass daily loss check."""
        validator = self._make_validator(max_daily_loss=1)
        validator.record_pnl("strat_a", -999_999_999)
        intent = _make_intent(intent_type=IntentType.CANCEL)
        ok, reason = validator.check(intent)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_resets_on_date_change(self) -> None:
        """Accumulated losses must reset when the calendar date rolls over."""
        validator = self._make_validator(max_daily_loss=500_000_000)
        validator.record_pnl("strat_a", -600_000_000)

        # Sanity: currently over limit
        intent = _make_intent()
        ok, _ = validator.check(intent)
        self.assertFalse(ok)

        # Simulate date rollover by patching _today_midnight_ns to return tomorrow
        tomorrow_midnight_ns = validator._current_reset_boundary_ns + DailyLossLimitValidator._NS_PER_DAY
        with patch.object(
            DailyLossLimitValidator,
            "_current_boundary_ns",
            return_value=tomorrow_midnight_ns,
        ):
            ok, reason = validator.check(intent)

        # After reset, accumulated losses are cleared — should pass
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")
        self.assertEqual(validator._accumulated_loss, {})

    def test_per_strategy_loss_limit_override(self) -> None:
        """Per-strategy max_daily_loss overrides global default."""
        strategies = {"strat_a": {"max_daily_loss": 100_000_000}}
        validator = self._make_validator(max_daily_loss=500_000_000, strategies=strategies)

        # strat_a hits its own tighter limit of 100M
        validator.record_pnl("strat_a", -150_000_000)
        intent_a = _make_intent(strategy_id="strat_a")
        ok, reason = validator.check(intent_a)
        self.assertFalse(ok)
        self.assertIn("DAILY_LOSS_LIMIT_EXCEEDED", reason)

        # strat_b uses global default (500M) — 150M loss is fine
        validator.record_pnl("strat_b", -150_000_000)
        intent_b = _make_intent(strategy_id="strat_b")
        ok, reason = validator.check(intent_b)
        self.assertTrue(ok)

    def test_no_loss_with_only_gains(self) -> None:
        """Strategies with only gains should never be rejected."""
        validator = self._make_validator(max_daily_loss=1)
        validator.record_pnl("strat_a", 999_999_999)
        intent = _make_intent()
        ok, reason = validator.check(intent)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")


if __name__ == "__main__":
    unittest.main()
