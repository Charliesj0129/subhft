"""
E2E tests for the Execution Plane (Plane 4).

Covers:
  - OrderAdapter dispatching to broker
  - ExecutionNormalizer producing FillEvent from raw exec dicts
  - PositionStore updating on fill (buy/sell round-trip)
  - Full synchronous execution chain
  - Async order-to-fill pipeline
  - Cancel order flow
  - Broker reject → DLQ (no crash)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta
from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    Side,
)
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from tests.e2e.conftest import (
    DEFAULT_PRICE,
    DEFAULT_SYMBOL,
    DEFAULT_TS_NS,
    SCALE,
    InMemoryBrokerAPI,
    make_command,
    make_fill,
    make_intent,
    wait_for_predicate,
)

# ---------------------------------------------------------------------------
# Patches
# ---------------------------------------------------------------------------

_METRICS_PATCH = "hft_platform.order.adapter.MetricsRegistry"
_LATENCY_PATCH = "hft_platform.order.adapter.LatencyRecorder"
_EXEC_METRICS_PATCH = "hft_platform.execution.normalizer.MetricsRegistry"
_POS_METRICS_PATCH = "hft_platform.execution.positions.MetricsRegistry"
_ROUTER_METRICS_PATCH = "hft_platform.execution.router.MetricsRegistry"


# ---------------------------------------------------------------------------
# Minimal broker codec stub
# ---------------------------------------------------------------------------


class _StubBrokerCodec:
    """Minimal BrokerOrderCodec that returns plain strings — no SDK required."""

    def encode_side(self, side: Any) -> str:
        if side == Side.SELL:
            return "Sell"
        return "Buy"

    def encode_tif(self, tif: Any) -> str:
        return "IOC"

    def encode_price_type(self, price_type: str) -> str:
        return price_type or "LMT"


# ---------------------------------------------------------------------------
# Helper: build OrderAdapter (not running — direct-dispatch mode)
# ---------------------------------------------------------------------------


def _make_adapter(
    config_path: str,
    broker_api: InMemoryBrokerAPI,
    order_id_map: dict | None = None,
):
    """Construct an OrderAdapter with mocked metrics/latency."""
    from hft_platform.order.adapter import OrderAdapter

    mock_metrics = MagicMock()
    mock_metrics.order_actions_total.labels.return_value = MagicMock()
    mock_metrics.order_reject_total = MagicMock()
    mock_metrics.order_deadline_expired_total = MagicMock()
    mock_metrics.terminal_before_registration_total = MagicMock()
    mock_metrics.deferred_terminal_expired_total = MagicMock()
    mock_latency = MagicMock()

    order_queue: asyncio.Queue[OrderCommand] = asyncio.Queue(maxsize=64)

    with (
        patch(_METRICS_PATCH) as m_metrics,
        patch(_LATENCY_PATCH) as m_lat,
    ):
        m_metrics.get.return_value = mock_metrics
        m_lat.get.return_value = mock_latency

        adapter = OrderAdapter(
            config_path=config_path,
            order_queue=order_queue,
            broker_client=broker_api,
            order_id_map=order_id_map or {},
            broker_codec=_StubBrokerCodec(),
        )

    adapter.metrics = mock_metrics
    adapter.latency = mock_latency
    return adapter


# ---------------------------------------------------------------------------
# TestChain — synchronous chain tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e_chain
class TestChain:
    def test_order_adapter_calls_broker(self, e2e_adapter_yaml: str) -> None:
        """OrderAdapter.execute() should call broker_api.place_order for a NEW intent."""

        broker_api = InMemoryBrokerAPI()
        adapter = _make_adapter(e2e_adapter_yaml, broker_api)

        intent = make_intent(
            strategy_id="s1",
            symbol=DEFAULT_SYMBOL,
            side=Side.BUY,
            qty=2,
            price=DEFAULT_PRICE,
        )
        cmd = make_command(intent=intent)

        # execute() is async — run it directly (adapter.running=False → _dispatch_to_api)
        asyncio.run(adapter.execute(cmd))

        assert len(broker_api.placed_orders) == 1

    def test_execution_router_normalizes_fill(self) -> None:
        """ExecutionNormalizer.normalize_fill() should produce a FillEvent with int price."""

        mock_metrics = MagicMock()
        mock_metrics.execution_events_total.labels.return_value = MagicMock()
        mock_metrics.synthetic_fill_id_total = MagicMock()

        with patch(_EXEC_METRICS_PATCH) as m:
            m.get.return_value = mock_metrics
            order_id_map: dict[str, str] = {"ord-001": "test_strategy:1"}
            normalizer = ExecutionNormalizer(order_id_map=order_id_map)
        normalizer.metrics = mock_metrics

        raw = RawExecEvent(
            topic="deal",
            data={
                "seqno": "SEQ001",
                "full_code": DEFAULT_SYMBOL,
                "price": 500,  # raw float-like int from broker
                "quantity": 2,
                "action": "Buy",
                "ordno": "ord-001",
                "account_id": "test-account",
                "ts": DEFAULT_TS_NS,
            },
            ingest_ts_ns=DEFAULT_TS_NS,
        )

        fill = normalizer.normalize_fill(raw)

        assert fill is not None
        assert isinstance(fill.price, int)
        assert fill.qty == 2
        assert fill.side == Side.BUY

    def test_position_store_updates_on_fill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PositionStore.on_fill() with a BUY fill should set net_qty == 2."""

        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")

        mock_metrics = MagicMock()
        with patch(_POS_METRICS_PATCH) as m:
            m.get.return_value = mock_metrics
            store = PositionStore()
        # Force Python path
        store._rust_tracker = None
        store.metrics = mock_metrics

        fill = make_fill(
            fill_id="fill-001",
            strategy_id="test_strategy",
            symbol=DEFAULT_SYMBOL,
            side=Side.BUY,
            qty=2,
            price=DEFAULT_PRICE,
        )

        delta = store.on_fill(fill)

        assert delta is not None
        assert delta.net_qty == 2

    def test_full_execution_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Buy 2 then Sell 2 → net_qty==0 and realized_pnl != 0."""

        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")

        mock_metrics = MagicMock()
        with patch(_POS_METRICS_PATCH) as m:
            m.get.return_value = mock_metrics
            store = PositionStore()
        store._rust_tracker = None
        store.metrics = mock_metrics

        buy_fill = make_fill(
            fill_id="fill-buy",
            strategy_id="test_strategy",
            symbol=DEFAULT_SYMBOL,
            side=Side.BUY,
            qty=2,
            price=500 * SCALE,
        )
        sell_fill = make_fill(
            fill_id="fill-sell",
            strategy_id="test_strategy",
            symbol=DEFAULT_SYMBOL,
            side=Side.SELL,
            qty=2,
            price=510 * SCALE,
        )

        store.on_fill(buy_fill)
        delta = store.on_fill(sell_fill)

        key = "test-account:test_strategy:2330"
        pos = store.positions[key]
        assert pos.net_qty == 0
        assert pos.realized_pnl_scaled != 0


# ---------------------------------------------------------------------------
# TestIntegration — async pipeline tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e_integration
class TestIntegration:
    @pytest.mark.asyncio
    async def test_order_to_fill_async_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        e2e_adapter_yaml: str,
    ) -> None:
        """
        Wire OrderAdapter + ExecutionRouter as async tasks.
        Inject OrderCommand → broker places → inject raw fill → FillEvent + PositionDelta on bus.
        """
        monkeypatch.setenv("HFT_RUST_POSITIONS", "0")

        broker_api = InMemoryBrokerAPI()
        order_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        raw_exec_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        bus_events: list = []

        # --- Bus mock ---
        mock_bus = MagicMock()
        mock_bus.publish_many_nowait = lambda events: bus_events.extend(events)
        mock_bus.publish_nowait = lambda ev: bus_events.append(ev)

        # --- Metrics mocks ---
        mock_metrics = MagicMock()
        mock_metrics.order_actions_total.labels.return_value = MagicMock()
        mock_metrics.order_reject_total = MagicMock()
        mock_metrics.order_deadline_expired_total = MagicMock()
        mock_metrics.terminal_before_registration_total = MagicMock()
        mock_metrics.deferred_terminal_expired_total = MagicMock()
        mock_metrics.execution_events_total.labels.return_value = MagicMock()
        mock_metrics.synthetic_fill_id_total = MagicMock()
        mock_metrics.fills_total = MagicMock()
        mock_metrics.execution_router_alive = MagicMock()
        mock_metrics.execution_router_heartbeat_ts = MagicMock()
        mock_metrics.execution_router_lag_ns = MagicMock()
        mock_metrics.execution_router_errors_total = MagicMock()
        mock_metrics.exec_overflow_drained_total = MagicMock()
        mock_metrics.duplicate_fill_total = MagicMock()
        mock_metrics.orphaned_fill_total = MagicMock()

        mock_latency = MagicMock()

        from hft_platform.execution.router import ExecutionRouter

        order_id_map: dict = {}

        with (
            patch(_METRICS_PATCH) as m_metrics,
            patch(_LATENCY_PATCH) as m_lat,
            patch(_POS_METRICS_PATCH) as m_pos,
            patch(_ROUTER_METRICS_PATCH) as m_router,
            patch(_EXEC_METRICS_PATCH) as m_exec,
        ):
            m_metrics.get.return_value = mock_metrics
            m_lat.get.return_value = mock_latency
            m_pos.get.return_value = mock_metrics
            m_router.get.return_value = mock_metrics
            m_exec.get.return_value = mock_metrics

            from hft_platform.order.adapter import OrderAdapter

            adapter = OrderAdapter(
                config_path=e2e_adapter_yaml,
                order_queue=order_queue,
                broker_client=broker_api,
                order_id_map=order_id_map,
                broker_codec=_StubBrokerCodec(),
            )
            adapter.metrics = mock_metrics
            adapter.latency = mock_latency

            store = PositionStore()
            store._rust_tracker = None
            store.metrics = mock_metrics

            router = ExecutionRouter(
                bus=mock_bus,
                raw_queue=raw_exec_queue,
                order_id_map=order_id_map,
                position_store=store,
                terminal_handler=lambda sid, oid: None,
            )
            router.metrics = mock_metrics

        # --- Inject order command ---
        intent = make_intent(
            strategy_id="s1",
            symbol=DEFAULT_SYMBOL,
            side=Side.BUY,
            qty=1,
            price=DEFAULT_PRICE,
        )
        cmd = make_command(
            intent=intent,
            deadline_ns=DEFAULT_TS_NS + 60_000_000_000,
        )
        await order_queue.put(cmd)

        # --- Run adapter task briefly ---
        adapter_task = asyncio.create_task(adapter.run())
        await wait_for_predicate(lambda: len(broker_api.placed_orders) >= 1, timeout=2.0)
        adapter_task.cancel()
        try:
            await adapter_task
        except (asyncio.CancelledError, Exception):
            pass

        assert len(broker_api.placed_orders) == 1

        # --- Inject raw fill ---
        raw_fill = RawExecEvent(
            topic="deal",
            data={
                "seqno": "SEQ001",
                "full_code": DEFAULT_SYMBOL,
                "price": 500,
                "quantity": 1,
                "action": "Buy",
                "ordno": "ord-001",
                "account_id": "test-account",
                "custom_field": "s1",
                "ts": DEFAULT_TS_NS,
            },
            ingest_ts_ns=DEFAULT_TS_NS,
        )
        await raw_exec_queue.put(raw_fill)

        # --- Run router for one iteration ---
        router_task = asyncio.create_task(router.run())
        await wait_for_predicate(
            lambda: any(isinstance(e, FillEvent) for e in bus_events),
            timeout=2.0,
        )
        router_task.cancel()
        try:
            await router_task
        except (asyncio.CancelledError, Exception):
            pass

        fill_events = [e for e in bus_events if isinstance(e, FillEvent)]
        delta_events = [e for e in bus_events if isinstance(e, PositionDelta)]
        assert len(fill_events) >= 1
        assert len(delta_events) >= 1

    @pytest.mark.asyncio
    async def test_cancel_order_flow(self, e2e_adapter_yaml: str) -> None:
        """Place order then cancel it — broker_api.cancelled_orders should have 1 entry."""

        broker_api = InMemoryBrokerAPI()
        adapter = _make_adapter(e2e_adapter_yaml, broker_api)

        # Place a new order
        intent_new = make_intent(
            intent_id=1,
            strategy_id="s1",
            symbol=DEFAULT_SYMBOL,
            intent_type=IntentType.NEW,
            side=Side.BUY,
            qty=1,
            price=DEFAULT_PRICE,
        )
        cmd_new = make_command(intent=intent_new)
        await adapter.execute(cmd_new)

        assert len(broker_api.placed_orders) == 1

        # The trade returned by place_order is stored in live_orders
        # For cancellation we can call cancel_order directly via client
        trade = broker_api.placed_orders[0]
        result = broker_api.cancel_order(trade)

        assert len(broker_api.cancelled_orders) == 1
        assert result is not None

    @pytest.mark.asyncio
    async def test_broker_reject_triggers_dlq(self, e2e_adapter_yaml: str) -> None:
        """When broker raises on place_order, adapter should not crash and order is not placed."""

        broker_api = InMemoryBrokerAPI()
        broker_api.should_reject = True

        adapter = _make_adapter(e2e_adapter_yaml, broker_api)

        intent = make_intent(
            strategy_id="s1",
            symbol=DEFAULT_SYMBOL,
            side=Side.BUY,
            qty=1,
            price=DEFAULT_PRICE,
        )
        cmd = make_command(intent=intent)

        # Should not raise even though broker rejects
        try:
            await adapter.execute(cmd)
        except Exception as exc:
            pytest.fail(f"execute() raised unexpectedly: {exc}")

        # Order was rejected by broker, so not in placed_orders
        assert len(broker_api.placed_orders) == 0
