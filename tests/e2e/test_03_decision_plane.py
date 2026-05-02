"""
E2E tests for the Decision Plane (Plane 3).

Covers:
  - Strategy emitting OrderIntents from LOBStatsEvents
  - RiskEngine approving / rejecting intents
  - StormGuard halt blocking orders
  - Risk-queue async pipeline
  - Gateway path: LocalIntentChannel → GatewayService → RiskEngine → order_queue
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    OrderIntent,
    Side,
)
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy
from tests.e2e.conftest import (
    DEFAULT_PRICE,
    DEFAULT_SYMBOL,
    make_intent,
    make_lob_stats,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AUDIT_PATCH = "hft_platform.recorder.audit.get_audit_writer"
_METRICS_PATCH = "hft_platform.risk.engine.MetricsRegistry"
_LATENCY_PATCH = "hft_platform.risk.engine.LatencyRecorder"


def _make_risk_engine(config_path: str, intent_queue=None, order_queue=None, storm_guard=None):
    """Construct a RiskEngine with Rust disabled."""
    from hft_platform.risk.engine import RiskEngine

    if intent_queue is None:
        intent_queue = asyncio.Queue(maxsize=64)
    if order_queue is None:
        order_queue = asyncio.Queue(maxsize=64)
    return RiskEngine(
        config_path=config_path,
        intent_queue=intent_queue,
        order_queue=order_queue,
        storm_guard=storm_guard,
    )


# ---------------------------------------------------------------------------
# Stub strategy
# ---------------------------------------------------------------------------


class _StubStrategy(BaseStrategy):
    """Minimal strategy that emits one BUY intent on every on_stats call."""

    def __init__(self, strategy_id: str, symbols: list[str]):
        super().__init__(strategy_id=strategy_id, symbols=symbols)
        self._emitted: list[OrderIntent] = []

    def on_stats(self, event: LOBStatsEvent) -> None:  # type: ignore[override]
        intent = make_intent(
            strategy_id=self.strategy_id,
            symbol=event.symbol,
            side=Side.BUY,
            price=event.best_bid,
            qty=1,
        )
        self._emitted.append(intent)
        self._generated_intents.append(intent)


# ===========================================================================
# TestChain
# ===========================================================================


@pytest.mark.e2e_chain
class TestChain:
    def test_strategy_emits_intent(self, e2e_symbols_yaml: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Strategy.on_stats() must produce an OrderIntent with correct fields."""
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        strategy = _StubStrategy(strategy_id="test_strategy", symbols=[DEFAULT_SYMBOL])
        event = make_lob_stats(symbol=DEFAULT_SYMBOL, best_bid=DEFAULT_PRICE - 10_000)

        # handle_event requires a StrategyContext — call on_stats directly to
        # unit-test the signal logic in isolation.
        strategy._generated_intents.clear()
        strategy.on_stats(event)

        assert len(strategy._emitted) == 1
        intent = strategy._emitted[0]
        assert isinstance(intent, OrderIntent)
        assert intent.symbol == DEFAULT_SYMBOL
        assert isinstance(intent.price, int)
        assert intent.side == Side.BUY

    def test_risk_approve_valid_intent(
        self,
        e2e_risk_yaml: str,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RiskEngine.evaluate() must approve a valid intent and return reason_code=='OK'."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
        ):
            engine = _make_risk_engine(e2e_risk_yaml)
            intent = make_intent(price=100 * 10_000, qty=1)
            decision = engine.evaluate(intent)

        assert decision.approved is True
        assert decision.reason_code == "OK"

    def test_risk_reject_halt_state(
        self,
        e2e_risk_yaml: str,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """StormGuard in HALT state must cause RiskEngine.evaluate() to reject new intents."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
        ):
            from hft_platform.risk.storm_guard import StormGuard

            sg = StormGuard()
            sg.trigger_halt("e2e_test")

            engine = _make_risk_engine(e2e_risk_yaml, storm_guard=sg)
            intent = make_intent(price=100 * 10_000, qty=1)
            decision = engine.evaluate(intent)

        assert decision.approved is False

    def test_risk_reject_exposure_limit(
        self,
        tmp_path: Any,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A very low max_notional must cause RiskEngine to reject a large-notional intent."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        # max_notional: 1 → notional_scaled = 1 * scale(10000) = 10000
        # intent notional = 100*10000 * 100 = 100_000_000 > 10000, must reject
        tight_yaml = tmp_path / "tight_limits.yaml"
        tight_yaml.write_text(
            """\
global_defaults:
  max_position_lots: 100000
  max_order_qty: 100000
  max_daily_loss: 9999999999
  max_open_orders: 100
  max_notional: 1
"""
        )

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
        ):
            engine = _make_risk_engine(str(tight_yaml))
            intent = make_intent(price=100 * 10_000, qty=100)
            decision = engine.evaluate(intent)

        assert decision.approved is False


# ===========================================================================
# TestIntegration
# ===========================================================================


@pytest.mark.e2e_integration
class TestIntegration:
    @pytest.mark.asyncio
    async def test_strategy_to_risk_queue(
        self,
        e2e_risk_yaml: str,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An intent placed on risk_queue must produce an OrderCommand on order_queue."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        risk_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        order_queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
        ):
            engine = _make_risk_engine(e2e_risk_yaml, intent_queue=risk_queue, order_queue=order_queue)
            task = asyncio.create_task(engine.run())
            try:
                await risk_queue.put(make_intent(price=100 * 10_000, qty=1))
                cmd = await asyncio.wait_for(order_queue.get(), timeout=3.0)
                assert cmd is not None
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_rejection_does_not_reach_order_queue(
        self,
        tmp_path: Any,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rejected intent must not produce any OrderCommand on order_queue."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)

        tight_yaml = tmp_path / "tight_limits.yaml"
        tight_yaml.write_text(
            """\
global_defaults:
  max_position_lots: 100000
  max_order_qty: 100000
  max_daily_loss: 9999999999
  max_open_orders: 100
  max_notional: 1
"""
        )

        risk_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        order_queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
        ):
            engine = _make_risk_engine(str(tight_yaml), intent_queue=risk_queue, order_queue=order_queue)
            task = asyncio.create_task(engine.run())
            try:
                await risk_queue.put(make_intent(price=100 * 10_000, qty=100))
                await risk_queue.join()
                await asyncio.sleep(0.05)
                assert order_queue.empty()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_gateway_path_intent_to_command(
        self,
        e2e_risk_yaml: str,
        e2e_symbols_yaml: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Intent submitted to LocalIntentChannel must arrive as OrderCommand via GatewayService."""
        monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
        monkeypatch.setenv("SYMBOLS_CONFIG", e2e_symbols_yaml)
        monkeypatch.setenv("HFT_GATEWAY_METRICS", "0")

        from hft_platform.gateway.channel import LocalIntentChannel
        from hft_platform.gateway.dedup import IdempotencyStore
        from hft_platform.gateway.exposure import ExposureStore
        from hft_platform.gateway.policy import GatewayPolicy
        from hft_platform.gateway.service import GatewayService

        order_queue: asyncio.Queue = asyncio.Queue(maxsize=64)

        # Build a fake order adapter that has _api_queue
        fake_adapter = MagicMock()
        fake_adapter._api_queue = order_queue
        fake_adapter._supports_typed_command_ingress = False

        channel = LocalIntentChannel(maxsize=64)
        dedup = IdempotencyStore()
        exposure = ExposureStore()
        policy = GatewayPolicy()

        with (
            patch(_METRICS_PATCH),
            patch(_LATENCY_PATCH),
            patch(_AUDIT_PATCH, return_value=MagicMock()),
        ):
            from hft_platform.risk.storm_guard import StormGuard

            sg = StormGuard()
            engine = _make_risk_engine(e2e_risk_yaml, order_queue=order_queue)

            gw = GatewayService(
                channel=channel,
                risk_engine=engine,
                order_adapter=fake_adapter,
                exposure_store=exposure,
                dedup_store=dedup,
                storm_guard=sg,
                policy=policy,
            )

            gw_task = asyncio.create_task(gw.run())
            try:
                intent = make_intent(price=100 * 10_000, qty=1)
                channel.submit_nowait(intent)
                cmd = await asyncio.wait_for(order_queue.get(), timeout=3.0)
                assert cmd is not None
            finally:
                gw_task.cancel()
                try:
                    await gw_task
                except asyncio.CancelledError:
                    pass
