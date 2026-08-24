"""Five enforcement gaps in the risk layer, found 2026-08-23.

Each one is a place where two layers disagree about the same rule: StormGuard
approves a cover that the engine then rejects; a validator picks one limit
while the alert reports another; a reload path clears caches but not the
scalars those caches are built from.
"""

import asyncio
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.execution.positions import PositionStore
from hft_platform.risk.storm_guard import StormGuard, StormGuardState
from hft_platform.risk.validators import (
    DailyLossLimitValidator,
    PerSymbolNotionalValidator,
    PositionLimitValidator,
    PriceBandValidator,
)


def _engine(config: dict, **kwargs):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.dump(config, tmp)
    tmp.close()
    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.recorder.audit.get_audit_writer"),
    ):
        mock_mr.get.return_value = MagicMock()
        mock_lr.get.return_value = MagicMock()
        from hft_platform.risk.engine import RiskEngine

        return (
            RiskEngine(
                config_path=tmp.name,
                intent_queue=asyncio.Queue(),
                order_queue=asyncio.Queue(),
                **kwargs,
            ),
            tmp.name,
        )


_BASE_CONFIG = {
    "global_defaults": {"max_price_cap": 5000.0, "tick_size": 0.01, "price_band_ticks": 20},
    "strategies": {},
}


class TestReducingOrdersSurviveHalt:
    def test_cmd_reduces_position_works_with_an_object_provider(self) -> None:
        """bootstrap wires a PositionStore INSTANCE, which is not callable.

        `_cmd_reduces_position` called the raw provider slot, so every
        invocation raised TypeError into a bare except and returned False.
        Every DLQ cover was then classified as an opener and evicted the moment
        StormGuard escalated. The existing test passed a lambda, which is
        callable, so the object path was never exercised.
        """
        store = PositionStore()
        store.positions["ACC:S1:TXFE6"] = type("P", (), {"symbol": "TXFE6", "strategy_id": "S1", "net_qty": -2})()
        engine, _ = _engine(_BASE_CONFIG, position_provider=store)

        intent = OrderIntent(
            intent_id=1,
            strategy_id="S1",
            symbol="TXFE6",
            intent_type=IntentType.NEW,
            side=Side.BUY,  # buying back a short → reduces
            price=1_800_000,
            qty=1,
        )
        cmd = engine.create_command(intent)

        assert engine._cmd_reduces_position(cmd) is True

    def test_an_opening_order_is_not_treated_as_reducing(self) -> None:
        store = PositionStore()
        store.positions["ACC:S1:TXFE6"] = type("P", (), {"symbol": "TXFE6", "strategy_id": "S1", "net_qty": -2})()
        engine, _ = _engine(_BASE_CONFIG, position_provider=store)

        intent = OrderIntent(
            intent_id=1,
            strategy_id="S1",
            symbol="TXFE6",
            intent_type=IntentType.NEW,
            side=Side.SELL,  # deeper short → opening
            price=1_800_000,
            qty=1,
        )
        assert engine._cmd_reduces_position(engine.create_command(intent)) is False

    def test_a_reducing_order_counts_as_a_safety_order_under_halt(self) -> None:
        """StormGuard returns HALT_REDUCE_ONLY for these; the engine's
        post-approve gate must agree instead of rejecting them."""
        store = PositionStore()
        store.positions["ACC:S1:TXFE6"] = type("P", (), {"symbol": "TXFE6", "strategy_id": "S1", "net_qty": -2})()
        engine, _ = _engine(_BASE_CONFIG, position_provider=store)

        cover = engine.create_command(
            OrderIntent(
                intent_id=1,
                strategy_id="S1",
                symbol="TXFE6",
                intent_type=IntentType.NEW,
                side=Side.BUY,
                price=1_800_000,
                qty=1,
            )
        )
        opener = engine.create_command(
            OrderIntent(
                intent_id=2,
                strategy_id="S1",
                symbol="TXFE6",
                intent_type=IntentType.NEW,
                side=Side.SELL,
                price=1_800_000,
                qty=1,
            )
        )

        def is_safety(cmd) -> bool:
            return (
                cmd.intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                or engine._is_halt_exempt(cmd.intent.strategy_id)
                or engine._cmd_reduces_position(cmd)
            )

        assert is_safety(cover) is True
        assert is_safety(opener) is False

    def test_nothing_is_halt_exempt_by_default(self) -> None:
        """Why the gap had no fallback: the exempt list is empty in every
        shipped config, so `_is_halt_exempt` could never rescue a cover."""
        engine, _ = _engine(_BASE_CONFIG)
        assert engine._is_halt_exempt("S1") is False


class TestPerSymbolNotionalIsNotCoveredByRust:
    def test_per_symbol_notional_runs_even_when_rust_passes(self) -> None:
        """Rust is never told about `symbol_limits`, so if this validator is
        filtered out of `_rust_uncovered_validators` the per-symbol cap is
        enforced by nobody."""
        engine, _ = _engine(_BASE_CONFIG)
        kinds = {type(v) for v in engine._rust_uncovered_validators}
        assert PerSymbolNotionalValidator in kinds
        assert PositionLimitValidator in kinds
        assert DailyLossLimitValidator in kinds


class TestConfigReloadReDerivesScalars:
    def test_raising_the_global_price_cap_takes_effect(self) -> None:
        v = PriceBandValidator({"global_defaults": {"max_price_cap": 5000.0}})
        assert v._max_price_cap_raw == 5000.0

        v.reload({"global_defaults": {"max_price_cap": 20000.0}})

        assert v._max_price_cap_raw == 20000.0

    def test_tightening_the_intraday_hard_limit_takes_effect(self) -> None:
        cfg = {"global_defaults": {}, "intraday_pnl": {"hard_limit_ntd": 5000, "price_scale": 10000}}
        v = DailyLossLimitValidator(cfg)
        assert v._hard_limit_threshold_scaled == 50_000_000

        v.reload({"global_defaults": {}, "intraday_pnl": {"hard_limit_ntd": 1000, "price_scale": 10000}})

        assert v._hard_limit_threshold_scaled == 10_000_000

    def test_a_reload_does_not_clear_a_live_halt(self) -> None:
        """Thresholds are config; a latched HALT and accumulated PnL are not."""
        cfg = {"global_defaults": {}, "intraday_pnl": {"hard_limit_ntd": 5000, "price_scale": 10000}}
        v = DailyLossLimitValidator(cfg)
        v.halt_triggered = True
        v._accumulated_loss["S1"] = -123

        v.reload(cfg)

        assert v.halt_triggered is True
        assert v._accumulated_loss == {"S1": -123}

    def test_the_engine_reload_reaches_the_validators(self) -> None:
        engine, path = _engine(_BASE_CONFIG)
        new_config = {
            "global_defaults": {"max_price_cap": 20000.0, "tick_size": 0.01, "price_band_ticks": 20},
            "strategies": {},
        }
        with open(path, "w") as f:
            yaml.dump(new_config, f)

        engine.reload_config()

        band = next(v for v in engine.validators if isinstance(v, PriceBandValidator))
        assert band._max_price_cap_raw == 20000.0


class TestHaltAlertReportsTheLimitThatFired:
    """Drives ``RiskEngine._check_daily_loss_halt`` and reads what the
    dispatcher was actually handed -- not a re-implementation of the choice."""

    @staticmethod
    def _reported_limit(config: dict) -> int:
        recorded: list[tuple[int, int]] = []

        class Dispatcher:
            async def notify_daily_loss(self, total_pnl, limit):
                recorded.append((total_pnl, limit))

            async def notify_halt(self, reason):
                return None

        engine, _ = _engine(config, notification_dispatcher=Dispatcher())
        v = next(x for x in engine.validators if isinstance(x, DailyLossLimitValidator))
        v.halt_triggered = True

        async def drive():
            engine._check_daily_loss_halt()
            # the notify is scheduled as a task; let it run
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(drive())
        assert recorded, "notify_daily_loss was never called"
        return recorded[0][1]

    def test_the_alert_uses_the_intraday_hard_limit_when_enabled(self) -> None:
        """Prod enables intraday_pnl (hard_limit_ntd 5000), and both of the
        validator's halt paths pick `_hard_limit_threshold_scaled`. The alert
        hardcoded `_default_max_daily_loss`, so operators were told a number
        ~10x the one that actually tripped the HALT."""
        limit = self._reported_limit(
            {
                "global_defaults": {"max_daily_loss": 500_000_000},
                "strategies": {},
                "intraday_pnl": {"hard_limit_ntd": 5000, "price_scale": 10000},
            }
        )
        assert limit == 50_000_000

    def test_the_alert_falls_back_to_the_legacy_limit_when_disabled(self) -> None:
        limit = self._reported_limit({"global_defaults": {"max_daily_loss": 777}, "strategies": {}})
        assert limit == 777


class TestCrossedBookStampsTheCooldown:
    def test_a_crossed_book_storm_starts_the_cooldown_clock(self) -> None:
        """Every other storm-entry site stamps these; this one did not, so
        `_storm_entry_ts` stayed 0.0 and `(now - 0.0) >= 30` made the cooldown
        trivially satisfied on the very next tick."""
        guard = StormGuard()
        guard._storm_entry_ts = 0.0
        guard._de_escalate_count = 3

        guard.update_with_lob(mid_price_x2=3_600_000, spread_scaled=-50, imbalance=0.0, ts=1)

        assert guard.state >= StormGuardState.STORM
        assert guard._de_escalate_count == 0
        assert guard._storm_entry_ts == pytest.approx(time.monotonic(), abs=1.0)

    def test_the_cooldown_is_not_already_satisfied_after_a_crossed_book(self) -> None:
        guard = StormGuard()
        guard._storm_entry_ts = 0.0

        guard.update_with_lob(mid_price_x2=3_600_000, spread_scaled=-50, imbalance=0.0, ts=1)

        elapsed = time.monotonic() - guard._storm_entry_ts
        assert elapsed < guard._storm_cooldown_s
