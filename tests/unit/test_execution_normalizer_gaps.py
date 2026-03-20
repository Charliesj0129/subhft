"""Coverage gap tests for execution normalizer and router.

Covers:
1. normalize_order with non-dict "order" key
2. _resolve_from_order_id_map seqno vs ordno priority
3. Price scaling: price=0.0 -> 0 scaled int
4. strategy_id_resolvers custom injection
5. ExecutionRouter terminal handler returning a coroutine
6. ExecutionRouter lag metric observation with varying ingest_ts_ns
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import OrderEvent, OrderStatus, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw(topic: str = "order", data: dict | None = None, ts: int = 1_000_000) -> RawExecEvent:
    return RawExecEvent(topic=topic, data=data if data is not None else {}, ingest_ts_ns=ts)


def _normalizer(**kwargs: Any) -> ExecutionNormalizer:
    with patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg:
        mock_reg.get.return_value = MagicMock()
        return ExecutionNormalizer(**kwargs)


def _make_router(
    terminal_handler: Any = None,
    order_id_map: dict[str, str] | None = None,
    metrics: MagicMock | None = None,
) -> tuple:
    """Build an ExecutionRouter with patched metrics. Returns (router, bus, raw_queue, mock_metrics)."""
    from hft_platform.execution.router import ExecutionRouter

    bus = MagicMock()
    bus.publish_nowait = MagicMock()
    raw_queue: asyncio.Queue = asyncio.Queue()
    mock_metrics = metrics or MagicMock()

    with (
        patch("hft_platform.execution.router.MetricsRegistry") as mock_reg,
        patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg2,
    ):
        mock_reg.get.return_value = mock_metrics
        mock_reg2.get.return_value = mock_metrics
        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map=order_id_map or {},
            position_store=MagicMock(),
            terminal_handler=terminal_handler or MagicMock(),
        )
    return router, bus, raw_queue, mock_metrics


async def _run_router_processing(router: Any, raw_queue: asyncio.Queue, settle_s: float = 0.02) -> None:
    """Run the router until one event is processed, then stop."""

    async def _stop() -> None:
        await asyncio.sleep(settle_s)
        router.running = False
        # Sentinel to unblock the queue.get() call
        await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

    await asyncio.gather(router.run(), _stop())


def _make_order_event(
    order_id: str = "O1",
    strategy_id: str = "s1",
    status: OrderStatus = OrderStatus.FILLED,
    side: Side = Side.BUY,
) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        strategy_id=strategy_id,
        symbol="2330",
        status=status,
        submitted_qty=1,
        filled_qty=1 if status == OrderStatus.FILLED else 0,
        remaining_qty=0 if status == OrderStatus.FILLED else 1,
        price=5_000_000,
        side=side,
        ingest_ts_ns=1_000_000,
        broker_ts_ns=1_000_000,
    )


# ===========================================================================
# 1. normalize_order -- "order" key is not a dict
# ===========================================================================


class TestNormalizeOrderNonDictOrderKey:
    def test_returns_none_when_order_key_is_string(self) -> None:
        """If data['order'] exists but is not a dict, normalize_order returns None."""
        norm = _normalizer()
        raw = _make_raw(data={"order": "some-string-value", "status": "Submitted"})
        assert norm.normalize_order(raw) is None

    def test_returns_none_when_order_key_is_int(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={"order": 12345, "status": "Submitted"})
        assert norm.normalize_order(raw) is None

    def test_returns_none_when_order_key_is_list(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={"order": [1, 2, 3], "status": "Submitted"})
        assert norm.normalize_order(raw) is None

    def test_returns_none_when_order_key_is_none(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={"order": None, "status": "Submitted"})
        assert norm.normalize_order(raw) is None


# ===========================================================================
# 2. _resolve_from_order_id_map -- seqno vs ordno priority
# ===========================================================================


class TestResolveSeqnoVsOrdnoPriority:
    def test_ordno_resolved_first_when_both_present(self) -> None:
        """The resolver iterates [ord_no, seq_no, other_id]; ordno is tried first."""
        norm = _normalizer(order_id_map={"ORD-001": {"strategy_id": "strat_A", "intent_id": "i1"}})
        raw = _make_raw(data={"order": {"ordno": "ORD-001", "seqno": "SEQ-999"}})
        assert norm._resolve_from_order_id_map(raw) == "strat_A"

    def test_falls_back_to_seqno_when_ordno_not_mapped(self) -> None:
        norm = _normalizer(order_id_map={"SEQ-002": {"strategy_id": "strat_B"}})
        raw = _make_raw(data={"order": {"ordno": "UNMAPPED", "seqno": "SEQ-002"}})
        assert norm._resolve_from_order_id_map(raw) == "strat_B"

    def test_returns_none_when_neither_mapped(self) -> None:
        norm = _normalizer(order_id_map={})
        raw = _make_raw(data={"order": {"ordno": "X", "seqno": "Y"}})
        assert norm._resolve_from_order_id_map(raw) is None

    def test_ordno_at_top_level_when_no_order_dict(self) -> None:
        """ordno/seqno can also appear at the top level of data."""
        norm = _normalizer(order_id_map={"TOP_ORD": "strat_top"})
        raw = _make_raw(data={"ordno": "TOP_ORD", "seqno": "TOP_SEQ"})
        assert norm._resolve_from_order_id_map(raw) == "strat_top"

    def test_seqno_at_top_level_fallback(self) -> None:
        """When ordno is not mapped, seqno at top level is used."""
        norm = _normalizer(order_id_map={"TOP_SEQ": "strat_seq"})
        raw = _make_raw(data={"ordno": "UNMAPPED_ORD", "seqno": "TOP_SEQ"})
        assert norm._resolve_from_order_id_map(raw) == "strat_seq"


# ===========================================================================
# 3. Price scaling -- zero price
# ===========================================================================


class TestPriceScalingZero:
    def test_zero_float_price_order_scales_to_zero(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={
            "order": {"price": 0.0, "action": "Buy", "quantity": 10},
            "contract": {"code": "2330"},
            "status": "Submitted",
        })
        result = norm.normalize_order(raw)
        assert result is not None
        assert result.price == 0
        assert isinstance(result.price, int)

    def test_zero_int_price_order_scales_to_zero(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={
            "order": {"price": 0, "action": "Buy", "quantity": 10},
            "contract": {"code": "2330"},
            "status": "Submitted",
        })
        result = norm.normalize_order(raw)
        assert result is not None
        assert result.price == 0

    def test_zero_price_fill_scales_to_zero(self) -> None:
        norm = _normalizer()
        raw = _make_raw(topic="deal", data={
            "price": 0.0, "quantity": 5, "action": "Buy",
            "code": "2330", "seqno": "S1", "ordno": "O1",
        })
        result = norm.normalize_fill(raw)
        assert result is not None
        assert result.price == 0
        assert isinstance(result.price, int)


# ===========================================================================
# 4. strategy_id_resolvers custom injection
# ===========================================================================

_FILL_DATA = {
    "price": 100.0, "quantity": 5, "action": "Buy",
    "code": "2330", "seqno": "S1", "ordno": "O1",
}


class TestStrategyIdResolversCustomInjection:
    def test_custom_resolver_used_instead_of_defaults(self) -> None:
        """A custom resolver list replaces the default resolvers entirely."""
        norm = _normalizer(strategy_id_resolvers=[lambda _: "injected_strategy"])
        data = {**_FILL_DATA, "custom_field": "should_be_ignored"}
        fill = norm.normalize_fill(_make_raw(topic="deal", data=data))
        assert fill is not None
        assert fill.strategy_id == "injected_strategy"

    def test_custom_resolver_chain_falls_through(self) -> None:
        """If the first custom resolver returns None, the next one is tried."""
        call_log: list[str] = []

        def first(_: RawExecEvent) -> str | None:
            call_log.append("first")
            return None

        def second(_: RawExecEvent) -> str | None:
            call_log.append("second")
            return "second_strat"

        norm = _normalizer(strategy_id_resolvers=[first, second])
        fill = norm.normalize_fill(_make_raw(topic="deal", data=_FILL_DATA))
        assert fill is not None
        assert fill.strategy_id == "second_strat"
        assert call_log == ["first", "second"]

    def test_all_custom_resolvers_return_none_yields_unknown(self) -> None:
        norm = _normalizer(strategy_id_resolvers=[lambda _: None])
        fill = norm.normalize_fill(_make_raw(topic="deal", data=_FILL_DATA))
        assert fill is not None
        assert fill.strategy_id == "UNKNOWN"

    def test_custom_resolver_exception_caught_and_next_tried(self) -> None:
        """Resolver exceptions (ValueError, KeyError, etc.) are caught gracefully."""

        def broken(_: RawExecEvent) -> str | None:
            raise ValueError("broken")

        norm = _normalizer(strategy_id_resolvers=[broken, lambda _: "safe_strat"])
        fill = norm.normalize_fill(_make_raw(topic="deal", data=_FILL_DATA))
        assert fill is not None
        assert fill.strategy_id == "safe_strat"

    def test_custom_resolver_applied_to_order_normalization(self) -> None:
        """Custom resolvers work for normalize_order as well."""
        norm = _normalizer(strategy_id_resolvers=[lambda _: "order_custom"])
        raw = _make_raw(data={
            "order": {"price": 100, "action": "Buy", "quantity": 5},
            "contract": {"code": "2330"},
            "status": "Submitted",
        })
        result = norm.normalize_order(raw)
        assert result is not None
        assert result.strategy_id == "order_custom"


# ===========================================================================
# 5. ExecutionRouter -- terminal handler returning a coroutine
# ===========================================================================


class TestRouterTerminalHandlerCoroutine:
    @pytest.mark.asyncio
    async def test_async_callable_handler_is_scheduled(self) -> None:
        """When terminal_handler is an async callable, the router schedules the
        returned coroutine via create_task."""
        handler_called = asyncio.Event()

        async def async_handler(strategy_id: str, order_id: str) -> None:
            handler_called.set()

        router, _bus, raw_queue, _metrics = _make_router(terminal_handler=async_handler)

        with patch.object(
            router.normalizer, "normalize_order",
            side_effect=[_make_order_event(order_id="O1", status=OrderStatus.FILLED), None],
        ):
            await raw_queue.put(_make_raw(topic="order", data={}, ts=1_000_000))
            await _run_router_processing(router, raw_queue)
            await asyncio.sleep(0.02)  # let the scheduled task complete

        assert handler_called.is_set()

    @pytest.mark.asyncio
    async def test_async_on_terminal_state_method_is_scheduled(self) -> None:
        """When terminal_handler is an object with async on_terminal_state,
        the coroutine is scheduled."""
        method_called = asyncio.Event()

        class HandlerObj:
            async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
                method_called.set()

        router, _bus, raw_queue, _metrics = _make_router(terminal_handler=HandlerObj())

        with patch.object(
            router.normalizer, "normalize_order",
            side_effect=[_make_order_event(order_id="O2", status=OrderStatus.CANCELLED, side=Side.SELL), None],
        ):
            await raw_queue.put(_make_raw(topic="order", data={}, ts=1_000_000))
            await _run_router_processing(router, raw_queue)
            await asyncio.sleep(0.02)

        assert method_called.is_set()

    @pytest.mark.asyncio
    async def test_sync_callable_handler_is_called_directly(self) -> None:
        """When terminal_handler is a sync callable, it is called directly."""
        sync_handler = MagicMock()
        router, _bus, raw_queue, _metrics = _make_router(terminal_handler=sync_handler)

        with patch.object(
            router.normalizer, "normalize_order",
            side_effect=[_make_order_event(order_id="O3", strategy_id="s3", status=OrderStatus.FAILED), None],
        ):
            await raw_queue.put(_make_raw(topic="order", data={}, ts=1_000_000))
            await _run_router_processing(router, raw_queue)

        sync_handler.assert_called_once_with("s3", "O3")


# ===========================================================================
# 6. ExecutionRouter -- lag metric observation with varying ingest_ts_ns
# ===========================================================================


class TestRouterLagMetricObservation:
    @pytest.mark.asyncio
    async def test_lag_ns_observed_with_fixed_timestamps(self) -> None:
        """Verify execution_router_lag_ns.observe is called with (now_ns - ingest_ts_ns)."""
        router, _bus, raw_queue, mock_metrics = _make_router()

        ingest_ts = 500_000
        fixed_now_ns = 1_500_000

        with (
            patch.object(router.normalizer, "normalize_order", return_value=None),
            patch("hft_platform.execution.router.timebase") as mock_tb,
        ):
            mock_tb.now_ns.return_value = fixed_now_ns
            mock_tb.now_s.return_value = fixed_now_ns / 1e9
            await raw_queue.put(_make_raw(topic="order", data={}, ts=ingest_ts))
            await _run_router_processing(router, raw_queue)

        mock_metrics.execution_router_lag_ns.observe.assert_called_with(
            fixed_now_ns - ingest_ts
        )

    @pytest.mark.asyncio
    async def test_lag_not_observed_when_ingest_ts_is_zero(self) -> None:
        """When ingest_ts_ns is 0 (falsy), the lag metric should NOT be observed."""
        router, _bus, raw_queue, mock_metrics = _make_router()

        with (
            patch.object(router.normalizer, "normalize_order", return_value=None),
            patch("hft_platform.execution.router.timebase") as mock_tb,
        ):
            mock_tb.now_ns.return_value = 2_000_000
            mock_tb.now_s.return_value = 0.002
            await raw_queue.put(_make_raw(topic="order", data={}, ts=0))
            await _run_router_processing(router, raw_queue)

        mock_metrics.execution_router_lag_ns.observe.assert_not_called()

    @pytest.mark.asyncio
    async def test_larger_lag_for_older_ingest_ts(self) -> None:
        """Older ingest_ts produces a larger observed lag value."""
        router, _bus, raw_queue, mock_metrics = _make_router()

        fixed_now_ns = 10_000_000
        with (
            patch.object(router.normalizer, "normalize_order", return_value=None),
            patch("hft_platform.execution.router.timebase") as mock_tb,
        ):
            mock_tb.now_ns.return_value = fixed_now_ns
            mock_tb.now_s.return_value = fixed_now_ns / 1e9

            await raw_queue.put(_make_raw(topic="order", data={}, ts=fixed_now_ns - 1_000_000))
            await raw_queue.put(_make_raw(topic="order", data={}, ts=fixed_now_ns - 9_000_000))
            await _run_router_processing(router, raw_queue, settle_s=0.05)

        assert mock_metrics.execution_router_lag_ns.observe.call_count == 2
        lag_recent = mock_metrics.execution_router_lag_ns.observe.call_args_list[0][0][0]
        lag_old = mock_metrics.execution_router_lag_ns.observe.call_args_list[1][0][0]
        assert lag_recent == 1_000_000
        assert lag_old == 9_000_000
        assert lag_old > lag_recent
