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
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw(topic: str = "order", data: dict | None = None, ts: int = 1_000_000) -> RawExecEvent:
    return RawExecEvent(topic=topic, data=data if data is not None else {}, ingest_ts_ns=ts)


def _normalizer(**kwargs) -> ExecutionNormalizer:
    with patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg:
        m = MagicMock()
        mock_reg.get.return_value = m
        return ExecutionNormalizer(**kwargs)


# ===========================================================================
# 1. normalize_order — "order" key is not a dict
# ===========================================================================


class TestNormalizeOrderNonDictOrderKey:
    def test_returns_none_when_order_key_is_string(self) -> None:
        """If data['order'] exists but is not a dict, normalize_order returns None."""
        norm = _normalizer()
        raw = _make_raw(data={"order": "some-string-value", "status": "Submitted"})
        result = norm.normalize_order(raw)
        assert result is None

    def test_returns_none_when_order_key_is_int(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={"order": 12345, "status": "Submitted"})
        result = norm.normalize_order(raw)
        assert result is None

    def test_returns_none_when_order_key_is_list(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={"order": [1, 2, 3], "status": "Submitted"})
        result = norm.normalize_order(raw)
        assert result is None

    def test_returns_none_when_order_key_is_none(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={"order": None, "status": "Submitted"})
        result = norm.normalize_order(raw)
        assert result is None


# ===========================================================================
# 2. _resolve_from_order_id_map — seqno vs ordno priority
# ===========================================================================


class TestResolveSeqnoVsOrdnoPriority:
    def test_ordno_resolved_first_when_both_present(self) -> None:
        """The resolver iterates [ord_no, seq_no, other_id] — ordno is tried first."""
        order_id_map = {"ORD-001": {"strategy_id": "strat_A", "intent_id": "i1"}}
        norm = _normalizer(order_id_map=order_id_map)
        raw = _make_raw(
            data={
                "order": {"ordno": "ORD-001", "seqno": "SEQ-999"},
            }
        )
        result = norm._resolve_from_order_id_map(raw)
        assert result == "strat_A"

    def test_falls_back_to_seqno_when_ordno_not_mapped(self) -> None:
        order_id_map = {"SEQ-002": {"strategy_id": "strat_B"}}
        norm = _normalizer(order_id_map=order_id_map)
        raw = _make_raw(
            data={
                "order": {"ordno": "UNMAPPED", "seqno": "SEQ-002"},
            }
        )
        result = norm._resolve_from_order_id_map(raw)
        assert result == "strat_B"

    def test_returns_none_when_neither_mapped(self) -> None:
        norm = _normalizer(order_id_map={})
        raw = _make_raw(
            data={
                "order": {"ordno": "X", "seqno": "Y"},
            }
        )
        result = norm._resolve_from_order_id_map(raw)
        assert result is None

    def test_ordno_at_top_level_when_no_order_dict(self) -> None:
        """ordno/seqno can also appear at the top level of data (not nested in 'order')."""
        order_id_map = {"TOP_ORD": "strat_top"}
        norm = _normalizer(order_id_map=order_id_map)
        raw = _make_raw(
            data={
                "ordno": "TOP_ORD",
                "seqno": "TOP_SEQ",
            }
        )
        result = norm._resolve_from_order_id_map(raw)
        assert result == "strat_top"

    def test_seqno_at_top_level_fallback(self) -> None:
        """When ordno is not mapped, seqno at top level is used."""
        order_id_map = {"TOP_SEQ": "strat_seq"}
        norm = _normalizer(order_id_map=order_id_map)
        raw = _make_raw(
            data={
                "ordno": "UNMAPPED_ORD",
                "seqno": "TOP_SEQ",
            }
        )
        result = norm._resolve_from_order_id_map(raw)
        assert result == "strat_seq"


# ===========================================================================
# 3. Price scaling — zero price
# ===========================================================================


class TestPriceScalingZero:
    def test_zero_float_price_order_scales_to_zero(self) -> None:
        norm = _normalizer()
        raw = _make_raw(
            data={
                "order": {"price": 0.0, "action": "Buy", "quantity": 10},
                "contract": {"code": "2330"},
                "status": "Submitted",
            }
        )
        result = norm.normalize_order(raw)
        assert result is not None
        assert result.price == 0
        assert isinstance(result.price, int)

    def test_zero_int_price_order_scales_to_zero(self) -> None:
        norm = _normalizer()
        raw = _make_raw(
            data={
                "order": {"price": 0, "action": "Buy", "quantity": 10},
                "contract": {"code": "2330"},
                "status": "Submitted",
            }
        )
        result = norm.normalize_order(raw)
        assert result is not None
        assert result.price == 0

    def test_zero_price_fill_scales_to_zero(self) -> None:
        norm = _normalizer()
        raw = _make_raw(
            topic="deal",
            data={
                "price": 0.0,
                "quantity": 5,
                "action": "Buy",
                "code": "2330",
                "seqno": "S1",
                "ordno": "O1",
            },
        )
        result = norm.normalize_fill(raw)
        assert result is not None
        assert result.price == 0
        assert isinstance(result.price, int)


# ===========================================================================
# 4. strategy_id_resolvers custom injection
# ===========================================================================


class TestStrategyIdResolversCustomInjection:
    def test_custom_resolver_used_instead_of_defaults(self) -> None:
        """A custom resolver list replaces the default resolvers entirely."""

        def always_custom(raw: RawExecEvent) -> str | None:
            return "injected_strategy"

        norm = _normalizer(strategy_id_resolvers=[always_custom])
        raw = _make_raw(
            topic="deal",
            data={
                "price": 100.0,
                "quantity": 5,
                "action": "Buy",
                "code": "2330",
                "seqno": "S1",
                "ordno": "O1",
                "custom_field": "should_be_ignored",
            },
        )
        fill = norm.normalize_fill(raw)
        assert fill is not None
        # custom_field would normally be resolved by default resolver, but
        # we replaced the resolver list entirely
        assert fill.strategy_id == "injected_strategy"

    def test_custom_resolver_chain_falls_through(self) -> None:
        """If the first custom resolver returns None, the next one is tried."""
        call_log: list[str] = []

        def first(raw: RawExecEvent) -> str | None:
            call_log.append("first")
            return None

        def second(raw: RawExecEvent) -> str | None:
            call_log.append("second")
            return "second_strat"

        norm = _normalizer(strategy_id_resolvers=[first, second])
        raw = _make_raw(
            topic="deal",
            data={
                "price": 50.0,
                "quantity": 1,
                "action": "Buy",
                "code": "2330",
                "seqno": "S1",
                "ordno": "O1",
            },
        )
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "second_strat"
        assert call_log == ["first", "second"]

    def test_all_custom_resolvers_return_none_yields_unknown(self) -> None:
        def noop(raw: RawExecEvent) -> str | None:
            return None

        norm = _normalizer(strategy_id_resolvers=[noop])
        raw = _make_raw(
            topic="deal",
            data={
                "price": 50.0,
                "quantity": 1,
                "action": "Buy",
                "code": "2330",
                "seqno": "S1",
                "ordno": "O1",
            },
        )
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "UNKNOWN"

    def test_custom_resolver_exception_caught_and_next_tried(self) -> None:
        """Resolver exceptions (ValueError, KeyError, etc.) are caught gracefully."""

        def broken(raw: RawExecEvent) -> str | None:
            raise ValueError("broken")

        def fallback(raw: RawExecEvent) -> str | None:
            return "safe_strat"

        norm = _normalizer(strategy_id_resolvers=[broken, fallback])
        raw = _make_raw(
            topic="deal",
            data={
                "price": 50.0,
                "quantity": 1,
                "action": "Buy",
                "code": "2330",
                "seqno": "S1",
                "ordno": "O1",
            },
        )
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "safe_strat"

    def test_custom_resolver_applied_to_order_normalization(self) -> None:
        """Custom resolvers work for normalize_order as well."""

        def order_resolver(raw: RawExecEvent) -> str | None:
            return "order_custom"

        norm = _normalizer(strategy_id_resolvers=[order_resolver])
        raw = _make_raw(
            data={
                "order": {"price": 100, "action": "Buy", "quantity": 5},
                "contract": {"code": "2330"},
                "status": "Submitted",
            }
        )
        result = norm.normalize_order(raw)
        assert result is not None
        assert result.strategy_id == "order_custom"


# ===========================================================================
# 5. ExecutionRouter — terminal handler returning a coroutine
# ===========================================================================


class TestRouterTerminalHandlerCoroutine:
    @pytest.mark.asyncio
    async def test_async_callable_handler_is_scheduled(self) -> None:
        """When terminal_handler is an async callable and returns a coroutine,
        the router schedules it via create_task."""
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()

        handler_called = asyncio.Event()

        async def async_handler(strategy_id: str, order_id: str) -> None:
            handler_called.set()

        with (
            patch("hft_platform.execution.router.MetricsRegistry") as mock_reg,
            patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg2,
        ):
            m = MagicMock()
            mock_reg.get.return_value = m
            mock_reg2.get.return_value = m
            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map={},
                position_store=position_store,
                terminal_handler=async_handler,
            )

        order_evt = OrderEvent(
            order_id="O1",
            strategy_id="s1",
            symbol="2330",
            status=OrderStatus.FILLED,
            submitted_qty=1,
            filled_qty=1,
            remaining_qty=0,
            price=5_000_000,
            side=Side.BUY,
            ingest_ts_ns=1_000_000,
            broker_ts_ns=1_000_000,
        )
        with patch.object(router.normalizer, "normalize_order", side_effect=[order_evt, None]):
            raw = _make_raw(topic="order", data={}, ts=1_000_000)
            await raw_queue.put(raw)

            async def _stop_after_one() -> None:
                await asyncio.sleep(0.02)
                router.running = False
                await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

            await asyncio.gather(router.run(), _stop_after_one())
            # Allow the scheduled task to complete
            await asyncio.sleep(0.02)

        assert handler_called.is_set()

    @pytest.mark.asyncio
    async def test_async_on_terminal_state_method_is_scheduled(self) -> None:
        """When terminal_handler is a non-callable object with an async
        on_terminal_state method, the coroutine is scheduled."""
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()

        method_called = asyncio.Event()

        class HandlerObj:
            """Non-callable object with on_terminal_state method."""

            async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
                method_called.set()

        handler_obj = HandlerObj()
        # Ensure it is NOT callable at the top level (the router checks callable first)
        assert not callable(handler_obj) or hasattr(handler_obj, "on_terminal_state")

        with (
            patch("hft_platform.execution.router.MetricsRegistry") as mock_reg,
            patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg2,
        ):
            m = MagicMock()
            mock_reg.get.return_value = m
            mock_reg2.get.return_value = m
            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map={},
                position_store=position_store,
                terminal_handler=handler_obj,
            )

        order_evt = OrderEvent(
            order_id="O2",
            strategy_id="s2",
            symbol="2330",
            status=OrderStatus.CANCELLED,
            submitted_qty=1,
            filled_qty=0,
            remaining_qty=1,
            price=5_000_000,
            side=Side.SELL,
            ingest_ts_ns=1_000_000,
            broker_ts_ns=1_000_000,
        )
        with patch.object(router.normalizer, "normalize_order", side_effect=[order_evt, None]):
            raw = _make_raw(topic="order", data={}, ts=1_000_000)
            await raw_queue.put(raw)

            async def _stop_after_one() -> None:
                await asyncio.sleep(0.02)
                router.running = False
                await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

            await asyncio.gather(router.run(), _stop_after_one())
            await asyncio.sleep(0.02)

        assert method_called.is_set()

    @pytest.mark.asyncio
    async def test_sync_callable_handler_is_called_directly(self) -> None:
        """When terminal_handler is a sync callable, it is called directly (no task)."""
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()

        sync_handler = MagicMock()

        with (
            patch("hft_platform.execution.router.MetricsRegistry") as mock_reg,
            patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg2,
        ):
            m = MagicMock()
            mock_reg.get.return_value = m
            mock_reg2.get.return_value = m
            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map={},
                position_store=position_store,
                terminal_handler=sync_handler,
            )

        order_evt = OrderEvent(
            order_id="O3",
            strategy_id="s3",
            symbol="2330",
            status=OrderStatus.FAILED,
            submitted_qty=1,
            filled_qty=0,
            remaining_qty=1,
            price=5_000_000,
            side=Side.BUY,
            ingest_ts_ns=1_000_000,
            broker_ts_ns=1_000_000,
        )
        with patch.object(router.normalizer, "normalize_order", side_effect=[order_evt, None]):
            raw = _make_raw(topic="order", data={}, ts=1_000_000)
            await raw_queue.put(raw)

            async def _stop_after_one() -> None:
                await asyncio.sleep(0.02)
                router.running = False
                await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

            await asyncio.gather(router.run(), _stop_after_one())

        sync_handler.assert_called_once_with("s3", "O3")


# ===========================================================================
# 6. ExecutionRouter — lag metric observation with varying ingest_ts_ns
# ===========================================================================


class TestRouterLagMetricObservation:
    @pytest.mark.asyncio
    async def test_lag_ns_observed_with_fixed_timestamps(self) -> None:
        """Verify execution_router_lag_ns.observe is called with (now_ns - ingest_ts_ns)."""
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()

        mock_metrics = MagicMock()

        with (
            patch("hft_platform.execution.router.MetricsRegistry") as mock_reg,
            patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg2,
        ):
            mock_reg.get.return_value = mock_metrics
            mock_reg2.get.return_value = mock_metrics
            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map={},
                position_store=position_store,
                terminal_handler=MagicMock(),
            )

        with patch.object(router.normalizer, "normalize_order", return_value=None):
            ingest_ts = 500_000
            raw = _make_raw(topic="order", data={}, ts=ingest_ts)
            await raw_queue.put(raw)

            fixed_now_ns = 1_500_000

            with patch("hft_platform.execution.router.timebase") as mock_tb:
                mock_tb.now_ns.return_value = fixed_now_ns
                mock_tb.now_s.return_value = fixed_now_ns / 1e9

                async def _stop_after_one() -> None:
                    await asyncio.sleep(0.02)
                    router.running = False
                    await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

                await asyncio.gather(router.run(), _stop_after_one())

        mock_metrics.execution_router_lag_ns.observe.assert_called_with(fixed_now_ns - ingest_ts)

    @pytest.mark.asyncio
    async def test_lag_not_observed_when_ingest_ts_is_zero(self) -> None:
        """When ingest_ts_ns is 0 (falsy), the lag metric should NOT be observed."""
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()

        mock_metrics = MagicMock()

        with (
            patch("hft_platform.execution.router.MetricsRegistry") as mock_reg,
            patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg2,
        ):
            mock_reg.get.return_value = mock_metrics
            mock_reg2.get.return_value = mock_metrics
            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map={},
                position_store=position_store,
                terminal_handler=MagicMock(),
            )

        with patch.object(router.normalizer, "normalize_order", return_value=None):
            raw = _make_raw(topic="order", data={}, ts=0)
            await raw_queue.put(raw)

            with patch("hft_platform.execution.router.timebase") as mock_tb:
                mock_tb.now_ns.return_value = 2_000_000
                mock_tb.now_s.return_value = 0.002

                async def _stop_after_one() -> None:
                    await asyncio.sleep(0.02)
                    router.running = False
                    await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

                await asyncio.gather(router.run(), _stop_after_one())

        mock_metrics.execution_router_lag_ns.observe.assert_not_called()

    @pytest.mark.asyncio
    async def test_larger_lag_for_older_ingest_ts(self) -> None:
        """Verify that an older ingest_ts produces a larger observed lag value."""
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()

        mock_metrics = MagicMock()

        with (
            patch("hft_platform.execution.router.MetricsRegistry") as mock_reg,
            patch("hft_platform.execution.normalizer.MetricsRegistry") as mock_reg2,
        ):
            mock_reg.get.return_value = mock_metrics
            mock_reg2.get.return_value = mock_metrics
            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map={},
                position_store=position_store,
                terminal_handler=MagicMock(),
            )

        fixed_now_ns = 10_000_000
        with (
            patch.object(router.normalizer, "normalize_order", return_value=None),
            patch("hft_platform.execution.router.timebase") as mock_tb,
        ):
            mock_tb.now_ns.return_value = fixed_now_ns
            mock_tb.now_s.return_value = fixed_now_ns / 1e9

            # Recent event: 1ms ago
            raw_recent = _make_raw(topic="order", data={}, ts=fixed_now_ns - 1_000_000)
            # Old event: 9ms ago
            raw_old = _make_raw(topic="order", data={}, ts=fixed_now_ns - 9_000_000)
            await raw_queue.put(raw_recent)
            await raw_queue.put(raw_old)

            async def _stop_after_two() -> None:
                await asyncio.sleep(0.05)
                router.running = False
                await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

            await asyncio.gather(router.run(), _stop_after_two())

        assert mock_metrics.execution_router_lag_ns.observe.call_count == 2
        lag_recent = mock_metrics.execution_router_lag_ns.observe.call_args_list[0][0][0]
        lag_old = mock_metrics.execution_router_lag_ns.observe.call_args_list[1][0][0]
        assert lag_recent == 1_000_000
        assert lag_old == 9_000_000
        assert lag_old > lag_recent
