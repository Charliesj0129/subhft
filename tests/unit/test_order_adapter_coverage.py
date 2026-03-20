"""Comprehensive additional tests for OrderAdapter — dispatch, rate limit, circuit breaker, cancel."""

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from tests.factories import make_order_intent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(
    intent_id: int = 1,
    strategy_id: str = "s1",
    symbol: str = "2330",
    intent_type: IntentType = IntentType.NEW,
    side: Side = Side.BUY,
    price: int = 5000000,
    qty: int = 10,
    tif: TIF = TIF.ROD,
    target_order_id: str | None = None,
    trace_id: str = "",
    source_ts_ns: int = 0,
) -> OrderIntent:
    """Delegate to shared factory with local defaults."""
    return make_order_intent(
        intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        tif=tif,
        target_order_id=target_order_id,
        trace_id=trace_id,
        source_ts_ns=source_ts_ns,
    )


def _make_cmd(
    cmd_id: int = 1,
    intent: OrderIntent | None = None,
    deadline_ns: int | None = None,
    storm_guard_state: StormGuardState = StormGuardState.NORMAL,
    created_ns: int = 0,
) -> OrderCommand:
    if intent is None:
        intent = _make_intent()
    if deadline_ns is None:
        deadline_ns = timebase.now_ns() + 5_000_000_000  # 5s in future
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=deadline_ns,
        storm_guard_state=storm_guard_state,
        created_ns=created_ns,
    )


class FakeBrokerClient:
    """Minimal broker client mock with required methods."""

    def __init__(self):
        self.place_order_calls: list = []
        self.cancel_order_calls: list = []
        self.update_order_calls: list = []
        self.mode = "simulation"

    def place_order(self, **kwargs):
        self.place_order_calls.append(kwargs)
        return {"order_id": "ORD001", "seq_no": "SEQ001"}

    def cancel_order(self, trade, **kwargs):
        self.cancel_order_calls.append(trade)
        return {"status": "cancelled"}

    def update_order(self, trade, **kwargs):
        self.update_order_calls.append((trade, kwargs))
        return {"status": "amended"}

    def get_exchange(self, symbol: str) -> str:
        return "TSE"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in [
        "HFT_API_TIMEOUT_S",
        "HFT_API_GUARD_TIMEOUT_S",
        "HFT_API_MAX_INFLIGHT",
        "HFT_API_QUEUE_MAX",
        "HFT_API_COALESCE_WINDOW_S",
        "HFT_ORDER_ID_MAP_MAX_SIZE",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def order_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text("""
rate_limits:
  shioaji_soft_cap: 180
  shioaji_hard_cap: 250
  window_seconds: 10
circuit_breaker:
  threshold: 5
  timeout_seconds: 60
""")
    return str(cfg)


@pytest.fixture
def adapter(order_config):
    from hft_platform.order.adapter import OrderAdapter

    q = asyncio.Queue()
    client = FakeBrokerClient()
    return OrderAdapter(order_config, q, client)


# ===========================================================================
# Init Tests
# ===========================================================================


class TestInit:
    def test_basic_init(self, adapter):
        assert adapter.running is False
        assert adapter.order_id_map == {}
        assert adapter.live_orders == {}

    def test_rate_limiter_configured(self, adapter):
        assert adapter.rate_limiter.soft_cap == 180
        assert adapter.rate_limiter.hard_cap == 250

    def test_circuit_breaker_configured(self, adapter):
        assert adapter.circuit_breaker.threshold == 5

    def test_metadata_property(self, adapter):
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        assert isinstance(adapter.metadata, SymbolMetadata)

    def test_metadata_setter_updates_price_codec(self, adapter):
        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        new_meta = SymbolMetadata()
        adapter.metadata = new_meta
        assert adapter._metadata is new_meta


# ===========================================================================
# Validate Client Tests
# ===========================================================================


class TestValidateClient:
    def test_validate_new_order(self, adapter):
        intent = _make_intent(intent_type=IntentType.NEW)
        assert adapter._validate_client(intent) is True

    def test_validate_cancel_order(self, adapter):
        intent = _make_intent(intent_type=IntentType.CANCEL)
        assert adapter._validate_client(intent) is True

    def test_validate_amend_order(self, adapter):
        intent = _make_intent(intent_type=IntentType.AMEND)
        assert adapter._validate_client(intent) is True

    def test_validate_new_missing_place_order(self, adapter):
        adapter.client = MagicMock(spec=[])  # no place_order
        intent = _make_intent(intent_type=IntentType.NEW)
        assert adapter._validate_client(intent) is False

    def test_validate_cancel_missing_cancel_order(self, adapter):
        adapter.client = MagicMock(spec=["place_order", "get_exchange"])
        intent = _make_intent(intent_type=IntentType.CANCEL)
        assert adapter._validate_client(intent) is False


# ===========================================================================
# Execute / Dispatch Tests
# ===========================================================================


class TestExecute:
    @pytest.mark.asyncio
    async def test_execute_new_order(self, adapter):
        cmd = _make_cmd()
        adapter.running = False  # Direct dispatch
        await adapter.execute(cmd)
        assert len(adapter.client.place_order_calls) == 1
        assert "s1:1" in adapter.live_orders

    @pytest.mark.asyncio
    async def test_execute_cancel_order(self, adapter):
        # First place an order
        adapter.live_orders["s1:1"] = {"order_id": "ORD001"}
        cancel_intent = _make_intent(
            intent_id=2,
            intent_type=IntentType.CANCEL,
            target_order_id="1",
            price=0,
            qty=0,
        )
        cmd = _make_cmd(cmd_id=2, intent=cancel_intent)
        adapter.running = False
        await adapter.execute(cmd)
        assert len(adapter.client.cancel_order_calls) == 1

    @pytest.mark.asyncio
    async def test_execute_cancel_target_not_found(self, adapter):
        cancel_intent = _make_intent(
            intent_id=2,
            intent_type=IntentType.CANCEL,
            target_order_id="nonexistent",
        )
        cmd = _make_cmd(cmd_id=2, intent=cancel_intent)
        adapter.running = False
        await adapter.execute(cmd)
        assert len(adapter.client.cancel_order_calls) == 0

    @pytest.mark.asyncio
    async def test_execute_amend_order(self, adapter):
        adapter.live_orders["s1:1"] = {"order_id": "ORD001"}
        amend_intent = _make_intent(
            intent_id=2,
            intent_type=IntentType.AMEND,
            target_order_id="1",
            price=5100000,
        )
        cmd = _make_cmd(cmd_id=2, intent=amend_intent)
        adapter.running = False
        await adapter.execute(cmd)
        assert len(adapter.client.update_order_calls) == 1

    @pytest.mark.asyncio
    async def test_expired_deadline_skipped(self, adapter):
        cmd = _make_cmd(deadline_ns=1)  # already expired
        adapter.order_queue.put_nowait(cmd)
        adapter.running = True

        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.1)
        adapter.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(adapter.client.place_order_calls) == 0


# ===========================================================================
# Rate Limiting Tests
# ===========================================================================


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_rate_limit_blocks(self, adapter):
        # Fill up the rate window
        for _ in range(250):
            adapter.rate_limiter.record()
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        # Should be rate-limited — no broker call
        assert len(adapter.client.place_order_calls) == 0

    def test_check_rate_limit_ok(self, adapter):
        assert adapter.check_rate_limit() is True

    @pytest.mark.asyncio
    async def test_per_symbol_hard_limit(self, adapter):
        from hft_platform.order.rate_limiter import PerSymbolRateResult

        mock_limiter = MagicMock()
        mock_limiter.check = MagicMock(return_value=PerSymbolRateResult.HARD)
        adapter.per_symbol_rate_limiter = mock_limiter
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        assert len(adapter.client.place_order_calls) == 0


# ===========================================================================
# Circuit Breaker Tests
# ===========================================================================


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_breaker_open_rejects(self, adapter):
        adapter.circuit_breaker.open_until = timebase.now_s() + 60
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        assert len(adapter.client.place_order_calls) == 0

    @pytest.mark.asyncio
    async def test_per_strategy_circuit_breaker(self, adapter):
        mock_mgr = MagicMock()
        mock_mgr.is_open = MagicMock(return_value=True)
        adapter.strategy_cb_mgr = mock_mgr
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        assert len(adapter.client.place_order_calls) == 0


# ===========================================================================
# Shadow Mode Tests
# ===========================================================================


class TestShadowMode:
    @pytest.mark.asyncio
    async def test_shadow_mode_intercepts(self, adapter):
        mock_sink = MagicMock()
        mock_sink.enabled = True
        adapter.shadow_sink = mock_sink
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        mock_sink.intercept.assert_called_once()
        assert len(adapter.client.place_order_calls) == 0


# ===========================================================================
# Broker Error Handling Tests
# ===========================================================================


class TestBrokerErrors:
    @pytest.mark.asyncio
    async def test_broker_connection_error(self, adapter):
        adapter.client.place_order = MagicMock(side_effect=ConnectionError("refused"))
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        assert "s1:1" not in adapter.live_orders

    @pytest.mark.asyncio
    async def test_broker_timeout_error(self, adapter):
        adapter.client.place_order = MagicMock(side_effect=TimeoutError("timeout"))
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        assert "s1:1" not in adapter.live_orders

    @pytest.mark.asyncio
    async def test_broker_runtime_error(self, adapter):
        adapter.client.place_order = MagicMock(side_effect=RuntimeError("api error"))
        cmd = _make_cmd()
        adapter.running = False
        await adapter.execute(cmd)
        assert "s1:1" not in adapter.live_orders


# ===========================================================================
# Transient Error Detection Tests
# ===========================================================================


class TestTransientErrors:
    def test_connection_error_is_transient(self, adapter):
        assert adapter._is_transient_error(ConnectionError("reset")) is True

    def test_timeout_error_is_transient(self, adapter):
        assert adapter._is_transient_error(TimeoutError("timeout")) is True

    def test_connection_reset_is_transient(self, adapter):
        assert adapter._is_transient_error(ConnectionResetError("reset")) is True

    def test_value_error_not_transient(self, adapter):
        assert adapter._is_transient_error(ValueError("bad value")) is False

    def test_error_message_pattern_transient(self, adapter):
        assert adapter._is_transient_error(RuntimeError("connection reset by peer")) is True

    def test_generic_runtime_error_not_transient(self, adapter):
        assert adapter._is_transient_error(RuntimeError("unknown error")) is False


# ===========================================================================
# Coalescing Tests
# ===========================================================================


class TestCoalescing:
    def test_coalesce_key_new(self, adapter):
        cmd = _make_cmd()
        key = adapter._coalesce_key(cmd)
        assert key == ("new", "s1", "2330")

    def test_coalesce_key_cancel(self, adapter):
        intent = _make_intent(intent_type=IntentType.CANCEL, target_order_id="ord1")
        cmd = _make_cmd(intent=intent)
        key = adapter._coalesce_key(cmd)
        assert key == ("cancel", "s1", "ord1")

    def test_coalesce_key_amend(self, adapter):
        intent = _make_intent(intent_type=IntentType.AMEND, target_order_id="ord1")
        cmd = _make_cmd(intent=intent)
        key = adapter._coalesce_key(cmd)
        assert key == ("amend", "s1", "ord1")

    def test_store_pending_cancel_removes_amend(self, adapter):
        amend_intent = _make_intent(intent_type=IntentType.AMEND, target_order_id="ord1")
        amend_cmd = _make_cmd(intent=amend_intent)
        adapter._store_pending(amend_cmd)
        assert ("amend", "s1", "ord1") in adapter._api_pending

        cancel_intent = _make_intent(intent_type=IntentType.CANCEL, target_order_id="ord1")
        cancel_cmd = _make_cmd(cmd_id=2, intent=cancel_intent)
        adapter._store_pending(cancel_cmd)
        assert ("amend", "s1", "ord1") not in adapter._api_pending
        assert ("cancel", "s1", "ord1") in adapter._api_pending

    def test_store_pending_amend_dropped_if_cancel_exists(self, adapter):
        cancel_intent = _make_intent(intent_type=IntentType.CANCEL, target_order_id="ord1")
        cancel_cmd = _make_cmd(intent=cancel_intent)
        adapter._store_pending(cancel_cmd)

        amend_intent = _make_intent(intent_type=IntentType.AMEND, target_order_id="ord1")
        amend_cmd = _make_cmd(cmd_id=2, intent=amend_intent)
        adapter._store_pending(amend_cmd)
        # Amend should not be stored
        assert ("amend", "s1", "ord1") not in adapter._api_pending


# ===========================================================================
# Enqueue API Tests
# ===========================================================================


class TestEnqueueApi:
    @pytest.mark.asyncio
    async def test_enqueue_success(self, adapter):
        cmd = _make_cmd()
        await adapter._enqueue_api(cmd)
        assert adapter._api_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_full_queue_drops(self, adapter):
        adapter._api_queue = asyncio.Queue(maxsize=1)
        cmd1 = _make_cmd(cmd_id=1)
        cmd2 = _make_cmd(cmd_id=2)
        await adapter._enqueue_api(cmd1)
        await adapter._enqueue_api(cmd2)  # should drop
        assert adapter._api_queue.qsize() == 1


# ===========================================================================
# Drain and Cancel Tests
# ===========================================================================


class TestDrainAndCancel:
    @pytest.mark.asyncio
    async def test_drain_empty(self, adapter):
        cancelled = await adapter.drain_and_cancel()
        assert cancelled == 0

    @pytest.mark.asyncio
    async def test_drain_with_live_orders(self, adapter):
        adapter.live_orders["s1:1"] = {"order_id": "ORD001"}
        cancelled = await adapter.drain_and_cancel()
        assert cancelled == 1
        assert len(adapter.client.cancel_order_calls) == 1

    @pytest.mark.asyncio
    async def test_drain_cancel_failure(self, adapter):
        adapter.client.cancel_order = MagicMock(side_effect=RuntimeError("fail"))
        adapter.live_orders["s1:1"] = {"order_id": "ORD001"}
        cancelled = await adapter.drain_and_cancel()
        assert cancelled == 0


# ===========================================================================
# Order ID Map Tests
# ===========================================================================


class TestOrderIdMap:
    @pytest.mark.asyncio
    async def test_register_broker_ids(self, adapter):
        trade = {"seq_no": "S1", "ord_no": "O1", "order_id": "ID1"}
        await adapter._register_broker_ids("s1:1", trade)
        assert adapter.order_id_map.get("S1") == "s1:1"
        assert adapter.order_id_map.get("O1") == "s1:1"

    @pytest.mark.asyncio
    async def test_register_broker_ids_object(self, adapter):
        trade = MagicMock()
        trade.seq_no = "S1"
        trade.ord_no = "O1"
        trade.order_id = "ID1"
        trade.id = None
        trade.order = None
        await adapter._register_broker_ids("s1:1", trade)
        assert adapter.order_id_map.get("S1") == "s1:1"

    @pytest.mark.asyncio
    async def test_eviction_on_overflow(self, adapter):
        adapter._order_id_map_max_size = 5
        for i in range(5):
            adapter.order_id_map[f"key_{i}"] = f"val_{i}"
        trade = {"seq_no": "NEW1"}
        await adapter._register_broker_ids("s1:99", trade)
        # Should have evicted some entries
        assert len(adapter.order_id_map) <= 5


# ===========================================================================
# Terminal State Tests
# ===========================================================================


class TestTerminalState:
    @pytest.mark.asyncio
    async def test_on_terminal_state_removes_order(self, adapter):
        adapter.live_orders["s1:1"] = {"order_id": "ORD001"}
        await adapter.on_terminal_state("s1", "1")
        assert "s1:1" not in adapter.live_orders

    @pytest.mark.asyncio
    async def test_on_terminal_state_nonexistent(self, adapter):
        # Should not raise and live_orders should remain unchanged
        live_orders_before = dict(adapter.live_orders)
        await adapter.on_terminal_state("s1", "nonexistent")
        assert adapter.live_orders == live_orders_before


# ===========================================================================
# Broker Codec Tests
# ===========================================================================


class TestBrokerCodec:
    def test_encode_side_buy(self, adapter):
        result = adapter._broker_codec.encode_side(Side.BUY)
        assert result == "Buy"

    def test_encode_side_sell(self, adapter):
        result = adapter._broker_codec.encode_side(Side.SELL)
        assert result == "Sell"

    def test_encode_tif_rod(self, adapter):
        result = adapter._broker_codec.encode_tif(TIF.ROD)
        assert result == "ROD"

    def test_encode_tif_ioc(self, adapter):
        result = adapter._broker_codec.encode_tif(TIF.IOC)
        assert result == "IOC"

    def test_encode_tif_fok(self, adapter):
        result = adapter._broker_codec.encode_tif(TIF.FOK)
        assert result == "FOK"
