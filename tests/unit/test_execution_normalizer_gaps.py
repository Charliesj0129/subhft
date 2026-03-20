"""Tests for ExecutionNormalizer and ExecutionRouter gap coverage.

Covers edge cases in normalize_order, _resolve_from_order_id_map,
price scaling, router terminal handler dispatch, and graceful handling
of missing/malformed data.
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


# ===========================================================================
# 2. _resolve_from_order_id_map — seqno vs ordno priority
# ===========================================================================


class TestResolveSeqnoVsOrdnoPriority:
    def test_ordno_resolved_first_when_both_present(self) -> None:
        """The resolver iterates [ord_no, seq_no, other_id] — ordno is tried first.

        resolve_strategy_id_from_candidates calls resolve_strategy_id which
        returns the strategy_id portion (before ':') of the normalized order key.
        """
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


# ===========================================================================
# 3. Price scaling — zero
# ===========================================================================


class TestPriceScalingZero:
    def test_price_zero_scales_to_zero(self) -> None:
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


# ===========================================================================
# 4. Price scaling — normal value
# ===========================================================================


class TestPriceScalingNormal:
    def test_price_500_scales_to_5_000_000(self) -> None:
        norm = _normalizer()
        raw = _make_raw(
            data={
                "order": {"price": 500.0, "action": "Buy", "quantity": 5},
                "contract": {"code": "2330"},
                "status": "Submitted",
            }
        )
        result = norm.normalize_order(raw)
        assert result is not None
        # Default scale is 10000 → 500.0 * 10000 = 5_000_000
        assert result.price == 5_000_000


# ===========================================================================
# 5. Router — terminal_handler returns a coroutine
# ===========================================================================


class TestRouterTerminalHandlerCoroutine:
    @pytest.mark.asyncio
    async def test_coroutine_handler_is_awaited(self) -> None:
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()
        order_id_map: dict[str, str] = {}

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
                order_id_map=order_id_map,
                position_store=position_store,
                terminal_handler=async_handler,
            )

        # Build an OrderEvent with terminal status (FILLED=3)
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
            # Allow the created task to complete
            await asyncio.sleep(0.02)

        assert handler_called.is_set()


# ===========================================================================
# 6. Router — terminal_handler is sync
# ===========================================================================


class TestRouterTerminalHandlerSync:
    @pytest.mark.asyncio
    async def test_sync_handler_is_called(self) -> None:
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()
        order_id_map: dict[str, str] = {}

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
                order_id_map=order_id_map,
                position_store=position_store,
                terminal_handler=sync_handler,
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

        sync_handler.assert_called_once_with("s2", "O2")


# ===========================================================================
# 7. Router — lag metric observation
# ===========================================================================


class TestRouterLagMetricObservation:
    @pytest.mark.asyncio
    async def test_lag_ns_observed(self) -> None:
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

            fixed_now = 1_500_000

            with patch("hft_platform.execution.router.timebase") as mock_tb:
                mock_tb.now_ns.return_value = fixed_now
                mock_tb.now_s.return_value = fixed_now / 1e9

                async def _stop_after_one() -> None:
                    await asyncio.sleep(0.02)
                    router.running = False
                    await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

                await asyncio.gather(router.run(), _stop_after_one())

        mock_metrics.execution_router_lag_ns.observe.assert_called_with(fixed_now - ingest_ts)


# ===========================================================================
# 8. Normalize — missing fields don't crash
# ===========================================================================


class TestNormalizeMissingFieldsGraceful:
    def test_empty_dict_returns_order_event_with_defaults(self) -> None:
        """An empty data dict should not crash; returns an OrderEvent with defaults."""
        norm = _normalizer()
        raw = _make_raw(data={})
        result = norm.normalize_order(raw)
        # Empty dict is a valid dict, should produce an OrderEvent with default values
        assert result is not None
        assert result.symbol == "UNKNOWN"
        assert result.order_id == ""

    def test_missing_contract_uses_unknown_symbol(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data={"order": {"price": 100, "action": "Buy"}})
        result = norm.normalize_order(raw)
        assert result is not None
        assert result.symbol == "UNKNOWN"

    def test_non_dict_data_returns_none(self) -> None:
        norm = _normalizer()
        raw = _make_raw(data="not-a-dict")  # type: ignore[arg-type]
        result = norm.normalize_order(raw)
        assert result is None

    def test_normalize_fill_missing_fields(self) -> None:
        """normalize_fill with empty dict should not crash."""
        norm = _normalizer()
        raw = _make_raw(topic="deal", data={})
        result = norm.normalize_fill(raw)
        # Should produce a FillEvent with zero/empty defaults
        assert result is not None
        assert result.qty == 0
        assert result.price == 0


# ===========================================================================
# 9. Normalize fill — all fields present → correct FillEvent
# ===========================================================================


class TestNormalizeFillEventAllFields:
    def test_all_fields_produce_correct_fill(self) -> None:
        norm = _normalizer()
        raw = _make_raw(
            topic="deal",
            data={
                "seqno": "SEQ-100",
                "ordno": "ORD-200",
                "account_id": "acct-01",
                "code": "2330",
                "action": "Sell",
                "quantity": 50,
                "price": 600.0,
                "ts": 1_700_000_000_000_000_000,  # nanoseconds
            },
        )
        result = norm.normalize_fill(raw)
        assert result is not None
        assert isinstance(result, FillEvent)
        assert result.fill_id == "SEQ-100"
        assert result.order_id == "ORD-200"
        assert result.account_id == "acct-01"
        assert result.symbol == "2330"
        assert result.side == Side.SELL
        assert result.qty == 50
        # 600.0 * 10000 = 6_000_000
        assert result.price == 6_000_000
        assert result.fee == 0
        assert result.tax == 0


# ===========================================================================
# 10. Router — unknown order ID handled gracefully
# ===========================================================================


class TestRouterUnknownOrderId:
    @pytest.mark.asyncio
    async def test_unknown_order_id_still_publishes(self) -> None:
        """An order event with strategy_id=UNKNOWN still gets published (for orders)."""
        from hft_platform.execution.router import ExecutionRouter

        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue: asyncio.Queue = asyncio.Queue()
        position_store = MagicMock()

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
                terminal_handler=MagicMock(),
            )

        order_evt = OrderEvent(
            order_id="BROKER-XYZ",
            strategy_id="UNKNOWN",
            symbol="2330",
            status=OrderStatus.SUBMITTED,
            submitted_qty=10,
            filled_qty=0,
            remaining_qty=10,
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

        # Order events are always published even if strategy_id is UNKNOWN
        bus.publish_nowait.assert_called_once_with(order_evt)

    @pytest.mark.asyncio
    async def test_unknown_fill_goes_to_dlq(self) -> None:
        """A fill with strategy_id=UNKNOWN is routed to DLQ, not published."""
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

        fill_evt = FillEvent(
            fill_id="F1",
            account_id="acct",
            order_id="UNKNOWN-ORDER",
            strategy_id="UNKNOWN",
            symbol="2330",
            side=Side.BUY,
            qty=1,
            price=5_000_000,
            fee=0,
            tax=0,
            ingest_ts_ns=1_000_000,
            match_ts_ns=1_000_000,
        )
        mock_dlq = MagicMock()
        with (
            patch.object(router.normalizer, "normalize_fill", side_effect=[fill_evt, None]),
            patch.object(router.normalizer, "normalize_order", return_value=None),
            patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq", return_value=mock_dlq),
        ):
            raw = _make_raw(topic="deal", data={}, ts=1_000_000)
            await raw_queue.put(raw)

            async def _stop_after_one() -> None:
                await asyncio.sleep(0.02)
                router.running = False
                await raw_queue.put(_make_raw(topic="order", data={}, ts=0))

            await asyncio.gather(router.run(), _stop_after_one())

        mock_dlq.add.assert_called_once_with(fill_evt)
        mock_metrics.orphaned_fill_total.inc.assert_called_once()
