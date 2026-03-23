"""End-to-end trading day simulation tests.

Simulates a full trading day pipeline IN-MEMORY with NO external dependencies.
All prices are int x10000 per platform conventions.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.contracts.strategy import IntentType, OrderIntent, StormGuardState

pytestmark = [pytest.mark.system, pytest.mark.asyncio]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCALE = 10_000
_TS = 1_000_000_000  # base timestamp (1s in ns)


def _price(raw: int) -> int:
    """Convert human-readable price to scaled int."""
    return raw * _SCALE


def _make_intent(
    intent_id: int = 1,
    strategy_id: str = "strat_a",
    symbol: str = "2330",
    side: Side = Side.BUY,
    price: int = _price(100),
    qty: int = 1,
    intent_type: IntentType = IntentType.NEW,
    idempotency_key: str = "",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        timestamp_ns=_TS,
        idempotency_key=idempotency_key,
    )


def _make_fill(
    order_id: str = "ORD-001",
    strategy_id: str = "strat_a",
    symbol: str = "2330",
    side: Side = Side.BUY,
    price: int = _price(100),
    qty: int = 1,
    fee: int = 200,
    tax: int = 0,
    match_ts_ns: int = _TS,
) -> FillEvent:
    return FillEvent(
        fill_id=f"FILL-{order_id}",
        account_id="ACC-001",
        order_id=order_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=match_ts_ns,
        match_ts_ns=match_ts_ns,
    )


def _write_config(tmp_path, extra_config=None):
    """Write a minimal risk config YAML and return the path."""
    data = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": 10_000_000,
            "max_order_size": 1000,
            "max_position_lots": 1000,
            "max_daily_loss": 500_000_000,
        },
        "strategies": {},
        "storm_guard": {
            "warm_threshold": -200_000,
            "storm_threshold": -500_000,
            "halt_threshold": -1_000_000,
        },
    }
    if extra_config:
        data.update(extra_config)
    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(data))
    return str(cfg_path)


def _make_engine(tmp_path, monkeypatch, extra_config=None):
    """Create RiskEngine with all external deps mocked."""
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
    monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.setenv("HFT_STORMGUARD_STORM_COOLDOWN_S", "0")
    monkeypatch.setenv("HFT_STORMGUARD_DE_ESCALATE_N", "1")

    cfg_path = _write_config(tmp_path, extra_config)
    intent_q: asyncio.Queue = asyncio.Queue()
    order_q: asyncio.Queue = asyncio.Queue()

    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.risk.engine.get_audit_writer", return_value=MagicMock()),
        patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_vmr,
    ):
        mock_mr.get.return_value = None
        mock_lr.get.return_value = None
        mock_vmr.get.return_value = MagicMock()

        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(cfg_path, intent_q, order_q)
        engine.metrics = None
        engine.latency = None

    return engine, intent_q, order_q


def _make_position_store(monkeypatch):
    """Create PositionStore with Rust and metrics disabled."""
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    with patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()
        store._rust_tracker = None
        store.metrics = None
    return store


async def _run_engine_until_empty(engine, intent_q):
    """Run the risk engine loop until all intents are processed."""
    task = asyncio.create_task(engine.run())
    await intent_q.join()
    engine.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFullPipelineSingleSymbol:
    """Test 1: Buy/sell cycle -> risk approve -> fill -> position -> PnL."""

    async def test_buy_sell_cycle_pnl(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)
        store = _make_position_store(monkeypatch)

        # Submit buy intent
        buy_intent = _make_intent(intent_id=1, side=Side.BUY, price=_price(100), qty=10)
        await intent_q.put(buy_intent)
        await _run_engine_until_empty(engine, intent_q)

        # Risk should approve
        assert not order_q.empty()
        buy_cmd = await order_q.get()
        assert buy_cmd.intent.side == Side.BUY

        # Simulate fill
        buy_fill = _make_fill(side=Side.BUY, price=_price(100), qty=10, fee=100, tax=0)
        delta = store.on_fill(buy_fill)
        assert delta.net_qty == 10

        # Submit sell intent at higher price
        sell_intent = _make_intent(intent_id=2, side=Side.SELL, price=_price(105), qty=10)
        await intent_q.put(sell_intent)
        await _run_engine_until_empty(engine, intent_q)

        sell_cmd = await order_q.get()
        sell_fill = _make_fill(
            order_id="ORD-002",
            side=Side.SELL,
            price=_price(105),
            qty=10,
            fee=100,
            tax=300,
        )
        delta = store.on_fill(sell_fill)

        # Position should be flat
        assert delta.net_qty == 0
        # PnL = (105 - 100) * 10000 * 10 = 500_000
        assert delta.realized_pnl == _price(5) * 10


class TestFullPipelineMultiSymbol:
    """Test 2: 3 symbols processed simultaneously."""

    async def test_three_symbols(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)
        store = _make_position_store(monkeypatch)

        symbols = ["2330", "2317", "2454"]
        prices = [_price(100), _price(200), _price(50)]

        # Submit buy intents for all symbols
        for i, (sym, px) in enumerate(zip(symbols, prices)):
            intent = _make_intent(intent_id=i + 1, symbol=sym, side=Side.BUY, price=px, qty=5)
            await intent_q.put(intent)

        await _run_engine_until_empty(engine, intent_q)

        # All should be approved
        commands = []
        while not order_q.empty():
            commands.append(await order_q.get())
        assert len(commands) == 3

        # Process fills
        for i, (sym, px) in enumerate(zip(symbols, prices)):
            fill = _make_fill(
                order_id=f"ORD-{i}",
                symbol=sym,
                side=Side.BUY,
                price=px,
                qty=5,
                fee=50,
            )
            delta = store.on_fill(fill)
            assert delta.net_qty == 5
            assert delta.symbol == sym


class TestSessionBoundaryFlatPositions:
    """Test 3: Open mixed positions, close all to flat at session end."""

    async def test_close_all_flat(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)
        store = _make_position_store(monkeypatch)

        # Open: buy 2330, sell-short 2317
        fills = [
            _make_fill(order_id="O1", symbol="2330", side=Side.BUY, price=_price(100), qty=10),
            _make_fill(order_id="O2", symbol="2317", side=Side.SELL, price=_price(200), qty=5),
        ]
        for f in fills:
            store.on_fill(f)

        key_2330 = "ACC-001:strat_a:2330"
        key_2317 = "ACC-001:strat_a:2317"
        assert store.positions[key_2330].net_qty == 10
        assert store.positions[key_2317].net_qty == -5

        # Close all
        close_fills = [
            _make_fill(order_id="O3", symbol="2330", side=Side.SELL, price=_price(105), qty=10),
            _make_fill(order_id="O4", symbol="2317", side=Side.BUY, price=_price(195), qty=5),
        ]
        for f in close_fills:
            store.on_fill(f)

        assert store.positions[key_2330].net_qty == 0
        assert store.positions[key_2317].net_qty == 0


class TestPnLAccountingEndToEnd:
    """Test 4: Total PnL = sum of individual trade PnLs (integer exact)."""

    async def test_pnl_sum_exact(self, tmp_path, monkeypatch):
        store = _make_position_store(monkeypatch)

        # Trade 1: Buy 100 sell 110 -> profit 10*5 = 50 per unit scaled
        buy1 = _make_fill(order_id="O1", symbol="2330", side=Side.BUY, price=_price(100), qty=5, fee=50, tax=0)
        store.on_fill(buy1)
        sell1 = _make_fill(order_id="O2", symbol="2330", side=Side.SELL, price=_price(110), qty=5, fee=50, tax=100)
        d1 = store.on_fill(sell1)

        # Trade 2: Buy 200 sell 190 -> loss -10*3 = -30 per unit scaled
        buy2 = _make_fill(order_id="O3", symbol="2317", side=Side.BUY, price=_price(200), qty=3, fee=30, tax=0)
        store.on_fill(buy2)
        sell2 = _make_fill(order_id="O4", symbol="2317", side=Side.SELL, price=_price(190), qty=3, fee=30, tax=60)
        d2 = store.on_fill(sell2)

        key_2330 = "ACC-001:strat_a:2330"
        key_2317 = "ACC-001:strat_a:2317"

        pnl_2330 = store.positions[key_2330].realized_pnl_scaled
        pnl_2317 = store.positions[key_2317].realized_pnl_scaled

        # PnL 2330: (110-100)*10000*5 = 500_000
        assert pnl_2330 == _price(10) * 5
        # PnL 2317: (190-200)*10000*3 = -300_000
        assert pnl_2317 == _price(-10) * 3

        total = store.total_pnl
        assert total == pnl_2330 + pnl_2317


class TestRiskRejectionFlow:
    """Test 5: Zero/float price rejected, no position change."""

    async def test_zero_price_rejected(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)
        store = _make_position_store(monkeypatch)

        # Zero price intent
        bad_intent = _make_intent(intent_id=1, price=0, qty=5)
        await intent_q.put(bad_intent)
        await _run_engine_until_empty(engine, intent_q)

        # Order queue should be empty (rejected)
        assert order_q.empty()
        # No positions
        assert len(store.positions) == 0

    async def test_float_price_rejected(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)

        # Float price — create intent with float price via direct attribute set
        intent = _make_intent(intent_id=2, price=1000000, qty=5)
        # Override price with float to trigger FLOAT_PRICE rejection
        object.__setattr__(intent, "price", 100.5)

        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"


class TestStormGuardHaltMidSession:
    """Test 6: Drawdown triggers HALT -> orders blocked -> cancels allowed -> recovery."""

    async def test_halt_blocks_new_allows_cancel_then_recovers(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)

        # Trigger HALT via pnl drawdown
        engine.storm_guard.update_pnl(-1_500_000)  # Below halt threshold (-1_000_000)
        assert engine.storm_guard.state == StormGuardState.HALT

        # NEW order should be rejected
        new_intent = _make_intent(intent_id=1, side=Side.BUY, price=_price(100), qty=5)
        decision = engine.evaluate(new_intent)
        assert not decision.approved
        assert decision.reason_code == "STORMGUARD_HALT"

        # CANCEL should still be allowed
        cancel_intent = _make_intent(
            intent_id=2,
            intent_type=IntentType.CANCEL,
            side=Side.SELL,
            price=0,
            qty=0,
        )
        cancel_decision = engine.evaluate(cancel_intent)
        assert cancel_decision.approved

        # Recovery: PnL improves above all thresholds
        engine.storm_guard.update_pnl(0)
        assert engine.storm_guard.state == StormGuardState.NORMAL

        # New orders should now be approved again
        recovery_intent = _make_intent(intent_id=3, side=Side.BUY, price=_price(100), qty=5)
        recovery_decision = engine.evaluate(recovery_intent)
        assert recovery_decision.approved


class TestFeeAccumulationFullDay:
    """Test 7: Multiple fills with fees -> total fees match sum exactly."""

    async def test_total_fees_exact(self, tmp_path, monkeypatch):
        store = _make_position_store(monkeypatch)

        expected_total_fees = 0
        fills = [
            _make_fill(order_id="O1", side=Side.BUY, price=_price(100), qty=10, fee=150, tax=0),
            _make_fill(order_id="O2", side=Side.BUY, price=_price(101), qty=5, fee=75, tax=0),
            _make_fill(order_id="O3", side=Side.SELL, price=_price(105), qty=15, fee=225, tax=450),
        ]
        for fill in fills:
            expected_total_fees += fill.fee + fill.tax
            store.on_fill(fill)

        key = "ACC-001:strat_a:2330"
        actual_fees = store.positions[key].fees_scaled
        assert actual_fees == expected_total_fees
        assert actual_fees == 150 + 75 + 225 + 450  # 900


class TestPositionFlipDuringSession:
    """Test 8: Long -> Short transition with correct PnL."""

    async def test_long_to_short_flip(self, tmp_path, monkeypatch):
        store = _make_position_store(monkeypatch)

        # Open long: buy 10 @ 100
        buy = _make_fill(order_id="O1", side=Side.BUY, price=_price(100), qty=10, fee=0, tax=0)
        store.on_fill(buy)

        key = "ACC-001:strat_a:2330"
        assert store.positions[key].net_qty == 10

        # Sell 15 @ 110 -> close 10 long + open 5 short
        sell = _make_fill(order_id="O2", side=Side.SELL, price=_price(110), qty=15, fee=0, tax=0)
        delta = store.on_fill(sell)

        assert delta.net_qty == -5
        # PnL on closing 10 units: (110-100)*10000*10 = 1_000_000
        assert store.positions[key].realized_pnl_scaled == _price(10) * 10
        # Avg price for new short should be the sell price
        assert store.positions[key].avg_price_scaled == _price(110)


class TestMultipleStrategiesSameSymbol:
    """Test 9: 2 strategies, independent position tracking."""

    async def test_independent_tracking(self, tmp_path, monkeypatch):
        store = _make_position_store(monkeypatch)

        # Strategy A buys 2330
        fill_a = _make_fill(
            order_id="OA1",
            strategy_id="strat_a",
            symbol="2330",
            side=Side.BUY,
            price=_price(100),
            qty=10,
        )
        store.on_fill(fill_a)

        # Strategy B sells 2330
        fill_b = _make_fill(
            order_id="OB1",
            strategy_id="strat_b",
            symbol="2330",
            side=Side.SELL,
            price=_price(100),
            qty=5,
        )
        store.on_fill(fill_b)

        key_a = "ACC-001:strat_a:2330"
        key_b = "ACC-001:strat_b:2330"

        assert store.positions[key_a].net_qty == 10
        assert store.positions[key_b].net_qty == -5

        # Close strategy A: sell 10
        close_a = _make_fill(
            order_id="OA2",
            strategy_id="strat_a",
            symbol="2330",
            side=Side.SELL,
            price=_price(110),
            qty=10,
            fee=0,
            tax=0,
        )
        store.on_fill(close_a)

        # Close strategy B: buy 5
        close_b = _make_fill(
            order_id="OB2",
            strategy_id="strat_b",
            symbol="2330",
            side=Side.BUY,
            price=_price(95),
            qty=5,
            fee=0,
            tax=0,
        )
        store.on_fill(close_b)

        assert store.positions[key_a].net_qty == 0
        assert store.positions[key_b].net_qty == 0

        # Strategy A PnL: (110-100)*10000*10 = 1_000_000
        assert store.positions[key_a].realized_pnl_scaled == _price(10) * 10
        # Strategy B PnL (short): (100-95)*10000*5 = 250_000
        assert store.positions[key_b].realized_pnl_scaled == _price(5) * 5


class TestHighThroughput:
    """Test 10: 1000 intents processed without error."""

    async def test_1000_intents(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)

        num_intents = 1000
        for i in range(num_intents):
            intent = _make_intent(
                intent_id=i,
                price=_price(100 + (i % 50)),
                qty=1 + (i % 10),
            )
            await intent_q.put(intent)

        await _run_engine_until_empty(engine, intent_q)

        # All should be approved (prices within valid range)
        approved_count = 0
        while not order_q.empty():
            await order_q.get()
            approved_count += 1

        assert approved_count == num_intents


class TestOrderLifecycleComplete:
    """Test 11: Intent -> risk -> partial fills -> full fill."""

    async def test_partial_then_full_fill(self, tmp_path, monkeypatch):
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)
        store = _make_position_store(monkeypatch)

        # Submit intent for 100 qty
        intent = _make_intent(intent_id=1, side=Side.BUY, price=_price(100), qty=100)
        await intent_q.put(intent)
        await _run_engine_until_empty(engine, intent_q)

        cmd = await order_q.get()
        assert cmd.intent.qty == 100

        # Partial fill: 30
        fill1 = _make_fill(order_id="ORD-001", side=Side.BUY, price=_price(100), qty=30, fee=30)
        d1 = store.on_fill(fill1)
        assert d1.net_qty == 30

        # Partial fill: 50
        fill2 = _make_fill(order_id="ORD-001", side=Side.BUY, price=_price(100), qty=50, fee=50, match_ts_ns=_TS + 1)
        d2 = store.on_fill(fill2)
        assert d2.net_qty == 80

        # Final fill: 20
        fill3 = _make_fill(order_id="ORD-001", side=Side.BUY, price=_price(100), qty=20, fee=20, match_ts_ns=_TS + 2)
        d3 = store.on_fill(fill3)
        assert d3.net_qty == 100

        key = "ACC-001:strat_a:2330"
        assert store.positions[key].net_qty == 100
        assert store.positions[key].avg_price_scaled == _price(100)
        assert store.positions[key].fees_scaled == 100  # 30+50+20


class TestIdempotency:
    """Test 12: Duplicate intents with same key handled correctly."""

    async def test_duplicate_intents_processed(self, tmp_path, monkeypatch):
        """RiskEngine processes each intent independently — dedup is at gateway layer.

        This test verifies that the risk engine does not crash or produce
        incorrect results when seeing duplicate idempotency keys. The actual
        dedup enforcement is outside RiskEngine (gateway/DedupStore), so both
        intents pass risk evaluation.
        """
        engine, intent_q, order_q = _make_engine(tmp_path, monkeypatch)

        # Two intents with same idempotency key
        intent1 = _make_intent(intent_id=1, price=_price(100), qty=5, idempotency_key="DEDUP-001")
        intent2 = _make_intent(intent_id=2, price=_price(100), qty=5, idempotency_key="DEDUP-001")

        await intent_q.put(intent1)
        await intent_q.put(intent2)
        await _run_engine_until_empty(engine, intent_q)

        # Both pass risk (dedup is at gateway layer, not risk)
        commands = []
        while not order_q.empty():
            commands.append(await order_q.get())
        assert len(commands) == 2

        # Verify position store handles duplicate fills correctly
        store = _make_position_store(monkeypatch)

        fill1 = _make_fill(order_id="ORD-DUP1", side=Side.BUY, price=_price(100), qty=5, fee=50)
        fill2 = _make_fill(order_id="ORD-DUP2", side=Side.BUY, price=_price(100), qty=5, fee=50)

        store.on_fill(fill1)
        store.on_fill(fill2)

        key = "ACC-001:strat_a:2330"
        # Both fills accumulate
        assert store.positions[key].net_qty == 10
        assert store.positions[key].fees_scaled == 100  # 50+50
