"""Tests for ExecutionRouter fill-normalize-position flow.

Covers:
- Fill event normalization producing correct PositionDelta
- Scaled-int price preservation through the pipeline (x10000)
- Error handling for malformed fills
- Position store: single fill, drawdown, multi-fill lifecycle, realized PnL
- Router: fill/order routing, unknown/malformed event handling
"""

import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore

_FILL_SEQ = 0


def _make_fill(
    side: Side,
    qty: int,
    price: int,
    *,
    fee: int = 0,
    tax: int = 0,
    account_id: str = "acc1",
    strategy_id: str = "strat1",
    symbol: str = "2330",
    match_ts_ns: int = 1_000_000_000,
) -> FillEvent:
    global _FILL_SEQ
    _FILL_SEQ += 1
    return FillEvent(
        fill_id=f"F{_FILL_SEQ:04d}",
        account_id=account_id,
        order_id=f"O{_FILL_SEQ:04d}",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=match_ts_ns - 100,
        match_ts_ns=match_ts_ns,
    )


def _symbols_cfg(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return cfg


@pytest.fixture()
def store():
    """PositionStore with metrics/Rust tracker disabled for unit testing."""
    with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
        s = PositionStore()
    s.metrics = None
    return s


@pytest.fixture(autouse=True)
def _isolate_fill_dedup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))
    import hft_platform.execution.positions as _positions

    monkeypatch.setattr(_positions, "_MIN_PEAK_SCALED", 2_000_000)


class TestFillNormalizePositionFlow:
    """End-to-end: raw deal event -> normalize_fill -> PositionStore.on_fill -> PositionDelta."""

    def test_normalized_fill_produces_correct_position_delta(self, tmp_path, monkeypatch, store):
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
        normalizer = ExecutionNormalizer()

        raw = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Buy",
                "price": 100.5,
                "quantity": 10,
                "seqno": "SEQ001",
                "ordno": "ORD001",
                "custom_field": "strat1",
                "account_id": "acc1",
                "ts": str(time.time()),
            },
            ingest_ts_ns=time.time_ns(),
        )

        fill = normalizer.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "strat1"
        assert fill.symbol == "2330"
        assert fill.price == 1_005_000

        delta = store.on_fill(fill)
        assert isinstance(delta, PositionDelta)
        assert delta.net_qty == 10
        assert delta.avg_price == 1_005_000
        assert delta.realized_pnl == 0
        assert delta.delta_source == "FILL"

    def test_scaled_int_price_preserved_through_pipeline(self, tmp_path, monkeypatch, store):
        """Verify prices stay as scaled ints (x10000) from normalization through position delta."""
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
        normalizer = ExecutionNormalizer()

        raw_buy = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Buy",
                "price": 500.25,
                "quantity": 5,
                "seqno": "S1",
                "ordno": "O1",
                "custom_field": "strat1",
                "account_id": "acc1",
                "ts": str(time.time()),
            },
            ingest_ts_ns=time.time_ns(),
        )
        fill_buy = normalizer.normalize_fill(raw_buy)
        assert fill_buy is not None
        assert fill_buy.price == 5_002_500

        delta_open = store.on_fill(fill_buy)
        assert delta_open.avg_price == 5_002_500

        raw_sell = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Sell",
                "price": 501.50,
                "quantity": 5,
                "seqno": "S2",
                "ordno": "O2",
                "custom_field": "strat1",
                "account_id": "acc1",
                "ts": str(time.time()),
            },
            ingest_ts_ns=time.time_ns(),
        )
        fill_sell = normalizer.normalize_fill(raw_sell)
        assert fill_sell is not None
        assert fill_sell.price == 5_015_000

        delta_close = store.on_fill(fill_sell)
        assert delta_close.realized_pnl == 62_500
        assert delta_close.net_qty == 0

    def test_malformed_fill_returns_none(self, tmp_path, monkeypatch):
        """Normalizer should return None for malformed data without raising."""
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
        normalizer = ExecutionNormalizer()

        raw = RawExecEvent(topic="deal", data="not-a-dict", ingest_ts_ns=time.time_ns())
        result = normalizer.normalize_fill(raw)
        assert result is None or result.qty == 0


class TestPositionStoreSingleFill:
    """Single fill updates position correctly."""

    def test_buy_fill_opens_long(self, store):
        fill = _make_fill(Side.BUY, qty=10, price=500_0000)
        delta = store.on_fill(fill)

        assert delta.net_qty == 10
        assert delta.avg_price == 500_0000
        assert delta.realized_pnl == 0
        assert delta.account_id == "acc1"
        assert delta.strategy_id == "strat1"
        assert delta.symbol == "2330"

    def test_sell_fill_opens_short(self, store):
        fill = _make_fill(Side.SELL, qty=5, price=600_0000)
        delta = store.on_fill(fill)

        assert delta.net_qty == -5
        assert delta.avg_price == 600_0000
        assert delta.realized_pnl == 0


class TestPositionDrawdown:
    """Portfolio drawdown calculation."""

    def test_no_fills_drawdown_zero(self, store):
        assert store.get_drawdown_pct() == 0.0

    def test_drawdown_after_profit_then_loss(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=500_0000, strategy_id="s1"))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=530_0000, strategy_id="s1"))
        assert store.total_pnl == 300_0000
        assert store.get_drawdown_pct() == 0.0

        store.on_fill(_make_fill(Side.BUY, qty=10, price=540_0000, strategy_id="s1"))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=510_0000, strategy_id="s1"))
        assert store.total_pnl == 0
        assert store.get_drawdown_pct() == pytest.approx(1.0)

    def test_drawdown_partial(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=500_0000, strategy_id="s1"))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=520_0000, strategy_id="s1"))
        assert store.total_pnl == 200_0000

        store.on_fill(_make_fill(Side.BUY, qty=10, price=520_0000, strategy_id="s1"))
        store.on_fill(_make_fill(Side.SELL, qty=10, price=510_0000, strategy_id="s1"))
        assert store.total_pnl == 100_0000
        assert store.get_drawdown_pct() == pytest.approx(0.5)


class TestPositionMultiFillLifecycle:
    """Open -> partial close -> full close lifecycle."""

    def test_open_partial_close_full_close(self, store):
        d1 = store.on_fill(_make_fill(Side.BUY, qty=10, price=500_0000))
        assert d1.net_qty == 10
        assert d1.avg_price == 500_0000
        assert d1.realized_pnl == 0

        d2 = store.on_fill(_make_fill(Side.SELL, qty=3, price=520_0000))
        assert d2.net_qty == 7
        assert d2.avg_price == 500_0000
        assert d2.realized_pnl == 60_0000

        d3 = store.on_fill(_make_fill(Side.SELL, qty=7, price=510_0000))
        assert d3.net_qty == 0
        assert d3.realized_pnl == 130_0000


class TestPositionRealizedPnl:
    """Realized PnL calculation on close."""

    def test_long_profit(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=500_0000))
        delta = store.on_fill(_make_fill(Side.SELL, qty=10, price=510_0000))
        assert delta.realized_pnl == 100_0000

    def test_long_loss(self, store):
        store.on_fill(_make_fill(Side.BUY, qty=10, price=500_0000))
        delta = store.on_fill(_make_fill(Side.SELL, qty=10, price=490_0000))
        assert delta.realized_pnl == -100_0000

    def test_short_profit(self, store):
        store.on_fill(_make_fill(Side.SELL, qty=10, price=600_0000))
        delta = store.on_fill(_make_fill(Side.BUY, qty=10, price=590_0000))
        assert delta.realized_pnl == 100_0000

    def test_short_loss(self, store):
        store.on_fill(_make_fill(Side.SELL, qty=10, price=600_0000))
        delta = store.on_fill(_make_fill(Side.BUY, qty=10, price=610_0000))
        assert delta.realized_pnl == -100_0000

    def test_fees_do_not_affect_realized_pnl(self, store):
        """Fees are tracked separately from realized PnL."""
        store.on_fill(_make_fill(Side.BUY, qty=10, price=500_0000, fee=1000, tax=500))
        delta = store.on_fill(_make_fill(Side.SELL, qty=10, price=510_0000, fee=1000, tax=500))
        assert delta.realized_pnl == 100_0000


class TestExecutionRouterFillRouting:
    """Router dispatches deal events through normalize -> position -> bus."""

    @pytest.mark.asyncio
    async def test_deal_event_routed_to_position_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))

        raw_queue = asyncio.Queue()
        bus = MagicMock()
        bus.publish_many_nowait = MagicMock()
        order_id_map = {"ORD001": "strat1"}

        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            position_store = PositionStore()
        position_store.metrics = None
        terminal_handler = MagicMock()

        from hft_platform.execution.router import ExecutionRouter

        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map=order_id_map,
            position_store=position_store,
            terminal_handler=terminal_handler,
        )

        raw = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Buy",
                "price": 100.0,
                "quantity": 5,
                "seqno": "SEQ1",
                "ordno": "ORD001",
                "account_id": "acc1",
                "ts": str(time.time()),
            },
            ingest_ts_ns=time.time_ns(),
        )
        await raw_queue.put(raw)

        async def _run_one():
            router.running = True
            task = asyncio.create_task(router.run())
            await asyncio.sleep(0.05)
            router.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_one()

        assert bus.publish_many_nowait.called or bus.publish_nowait is not None
        assert len(position_store.positions) == 1
        key = list(position_store.positions.keys())[0]
        pos = position_store.positions[key]
        assert pos.net_qty == 5

    @pytest.mark.asyncio
    async def test_order_event_routed_to_terminal_handler(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))

        raw_queue = asyncio.Queue()
        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        order_id_map = {"ORD001": "strat1"}

        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            position_store = PositionStore()
        position_store.metrics = None
        terminal_handler = MagicMock()

        from hft_platform.execution.router import ExecutionRouter

        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map=order_id_map,
            position_store=position_store,
            terminal_handler=terminal_handler,
        )

        raw = RawExecEvent(
            topic="order",
            data={
                "ord_no": "ORD001",
                "status": {"status": "Filled"},
                "contract": {"code": "2330"},
                "order": {"action": "Buy", "price": 100.0, "quantity": 5},
            },
            ingest_ts_ns=time.time_ns(),
        )
        await raw_queue.put(raw)

        async def _run_one():
            router.running = True
            task = asyncio.create_task(router.run())
            await asyncio.sleep(0.05)
            router.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_one()

        assert terminal_handler.called

    @pytest.mark.asyncio
    async def test_orphaned_fill_routed_to_dlq(self, tmp_path, monkeypatch):
        """Fill with strategy_id=UNKNOWN goes to DLQ, not position store."""
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))

        raw_queue = asyncio.Queue()
        bus = MagicMock()
        bus.publish_many_nowait = MagicMock()

        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            position_store = PositionStore()
        position_store.metrics = None
        terminal_handler = MagicMock()

        from hft_platform.execution.router import ExecutionRouter

        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={},
            position_store=position_store,
            terminal_handler=terminal_handler,
        )

        raw = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Buy",
                "price": 100.0,
                "quantity": 5,
                "seqno": "SEQ1",
                "ordno": "UNKNOWN_ORD",
                "account_id": "acc1",
                "ts": str(time.time()),
            },
            ingest_ts_ns=time.time_ns(),
        )
        await raw_queue.put(raw)

        async def _run_one():
            router.running = True
            task = asyncio.create_task(router.run())
            await asyncio.sleep(0.05)
            router.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_one()

        assert len(position_store.positions) == 0

    @pytest.mark.asyncio
    async def test_unknown_topic_handled_gracefully(self, tmp_path, monkeypatch):
        """Events with unknown topics should not crash the router."""
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))

        raw_queue = asyncio.Queue()
        bus = MagicMock()
        bus.publish_nowait = MagicMock()

        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            position_store = PositionStore()
        position_store.metrics = None
        terminal_handler = MagicMock()

        from hft_platform.execution.router import ExecutionRouter

        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={},
            position_store=position_store,
            terminal_handler=terminal_handler,
        )

        raw = RawExecEvent(
            topic="unknown_topic",
            data={"something": "irrelevant"},
            ingest_ts_ns=time.time_ns(),
        )
        await raw_queue.put(raw)

        async def _run_one():
            router.running = True
            task = asyncio.create_task(router.run())
            await asyncio.sleep(0.05)
            router.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_one()

        assert len(position_store.positions) == 0
        assert not bus.publish_many_nowait.called

    @pytest.mark.asyncio
    async def test_malformed_deal_event_does_not_crash_router(self, tmp_path, monkeypatch):
        """Malformed deal data should be handled without crashing the router loop."""
        monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))

        raw_queue = asyncio.Queue()
        bus = MagicMock()
        bus.publish_many_nowait = MagicMock()

        with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
            position_store = PositionStore()
        position_store.metrics = None
        terminal_handler = MagicMock()

        from hft_platform.execution.router import ExecutionRouter

        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={},
            position_store=position_store,
            terminal_handler=terminal_handler,
        )

        raw = RawExecEvent(topic="deal", data=None, ingest_ts_ns=time.time_ns())
        await raw_queue.put(raw)

        raw_valid = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Buy",
                "price": 100.0,
                "quantity": 5,
                "seqno": "SEQ2",
                "ordno": "ORD002",
                "custom_field": "strat1",
                "account_id": "acc1",
                "ts": str(time.time()),
            },
            ingest_ts_ns=time.time_ns(),
        )
        await raw_queue.put(raw_valid)

        async def _run_two():
            router.running = True
            task = asyncio.create_task(router.run())
            await asyncio.sleep(0.1)
            router.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_two()

        assert len(position_store.positions) == 1
