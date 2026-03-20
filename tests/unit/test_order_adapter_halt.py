"""OrderAdapter HALT safety and gap tests.

Tests safety-critical order adapter paths:
1. HALT blocks NEW orders (via GatewayPolicy gate — the enforcement point)
2. HALT allows CANCEL orders (via GatewayPolicy)
3. place_order returns None handled gracefully (no crash, no state update)
4. order_id_map updated after successful placement
5. load_config failure handling (missing file, empty, malformed)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.gateway.policy import GatewayPolicy, GatewayPolicyMode
from hft_platform.order.rate_limiter import PerSymbolRateResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "2330",
    price: int = 5_950_000,
    qty: int = 1,
    strategy_id: str = "test_strat",
    **overrides,
) -> OrderIntent:
    defaults = {
        "intent_id": 1,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "intent_type": intent_type,
        "side": Side.BUY,
        "price": price,
        "qty": qty,
        "tif": TIF.LIMIT,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def _make_cmd(
    intent: OrderIntent,
    storm_guard_state: StormGuardState = StormGuardState.NORMAL,
    deadline_ns: int | None = None,
    cmd_id: int = 1,
) -> OrderCommand:
    if deadline_ns is None:
        deadline_ns = timebase.now_ns() + 5_000_000_000
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=deadline_ns,
        storm_guard_state=storm_guard_state,
        created_ns=timebase.now_ns(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_deps():
    """Mock heavy infrastructure dependencies (metrics, latency, DLQ)."""
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics_mock = MagicMock()
        metrics_mock.order_reject_total = MagicMock()
        metrics_mock.order_actions_total = MagicMock()
        metrics_mock.order_actions_total.labels.return_value = MagicMock()
        mm.get.return_value = metrics_mock
        ml.get.return_value = MagicMock()
        dlq_mock = AsyncMock()
        md.return_value = dlq_mock
        yield


@pytest.fixture
def tmp_config(tmp_path):
    cfg_file = tmp_path / "order_config.yaml"
    cfg_file.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg_file)


class _StubMetadata:
    """Minimal metadata stub returning sane defaults for order dispatch."""

    def exchange(self, symbol: str) -> str:
        return "TSE"

    def product_type(self, symbol: str) -> str | None:
        return None

    def order_params(self, symbol: str) -> dict:
        return {}

    def price_scale(self, symbol: str) -> int:
        return 10000


@pytest.fixture
def adapter(tmp_config):
    from hft_platform.order.adapter import OrderAdapter

    q = asyncio.Queue()
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock(id="T1", seq_no="S1", ord_no="O1", order=None))
    client.get_exchange = MagicMock(return_value="TSE")
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    oa = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        shioaji_client=client,
    )
    oa.metadata = _StubMetadata()
    oa.per_symbol_rate_limiter = MagicMock()
    oa.per_symbol_rate_limiter.check.return_value = PerSymbolRateResult.OK
    oa.per_symbol_rate_limiter.record = MagicMock()
    oa.strategy_cb_mgr = MagicMock()
    oa.strategy_cb_mgr.is_open.return_value = False
    oa.circuit_breaker = MagicMock()
    oa.circuit_breaker.is_open.return_value = False
    oa.rate_limiter = MagicMock()
    oa.rate_limiter.check.return_value = True
    oa.rate_limiter.record = MagicMock()
    oa.shadow_sink = MagicMock()
    oa.shadow_sink.enabled = False
    return oa


# ===========================================================================
# 1. HALT blocks NEW orders
# ===========================================================================


class TestHaltBlocksNewOrders:
    """GatewayPolicy — the HALT enforcement mechanism — blocks NEW intents.

    Architecture note: OrderAdapter.execute() does NOT check storm_guard_state.
    HALT gating is enforced upstream by GatewayPolicy / RiskEngine. These tests
    verify the enforcement point and document the adapter's passthrough behavior.
    """

    def test_gateway_policy_halt_blocks_new(self):
        """GatewayPolicy in HALT mode rejects NEW intents."""
        policy = GatewayPolicy()
        policy.set_halt()
        assert policy.mode == GatewayPolicyMode.HALT

        intent = _make_intent(intent_type=IntentType.NEW)
        allowed, reason = policy.gate(intent, StormGuardState.HALT)

        assert allowed is False
        assert reason == "HALT"

    def test_gateway_policy_halt_blocks_amend(self):
        """GatewayPolicy in HALT mode also rejects AMEND intents."""
        policy = GatewayPolicy()
        policy.set_halt()

        intent = _make_intent(intent_type=IntentType.AMEND, target_order_id="X1")
        allowed, reason = policy.gate(intent, StormGuardState.HALT)

        assert allowed is False
        assert reason == "HALT"

    @pytest.mark.asyncio
    async def test_adapter_does_not_duplicate_halt_check(self, adapter):
        """The adapter dispatches commands regardless of storm_guard_state,
        because HALT enforcement is the upstream gateway's responsibility."""
        adapter.running = False
        intent = _make_intent(intent_type=IntentType.NEW)
        cmd = _make_cmd(intent, storm_guard_state=StormGuardState.HALT)

        await adapter.execute(cmd)

        # Adapter proceeds — HALT is enforced upstream, not here
        adapter.client.place_order.assert_called_once()

    def test_halt_state_value_preserved_on_command(self):
        """StormGuardState.HALT value is correctly carried on OrderCommand."""
        intent = _make_intent(intent_type=IntentType.NEW)
        cmd = _make_cmd(intent, storm_guard_state=StormGuardState.HALT)

        assert cmd.storm_guard_state == StormGuardState.HALT
        assert int(cmd.storm_guard_state) == 3


# ===========================================================================
# 2. HALT allows CANCEL orders
# ===========================================================================


class TestHaltAllowsCancel:
    """CANCEL orders must pass through even in HALT state."""

    def test_gateway_policy_halt_allows_cancel_default(self):
        """GatewayPolicy in HALT mode allows CANCEL intents by default."""
        policy = GatewayPolicy()
        policy.set_halt()

        intent = _make_intent(intent_type=IntentType.CANCEL, target_order_id="X1")
        allowed, reason = policy.gate(intent, StormGuardState.HALT)

        assert allowed is True
        assert reason == "OK"

    def test_gateway_policy_halt_cancel_configurable(self, monkeypatch):
        """HFT_GATEWAY_HALT_CANCEL=0 disables CANCEL passthrough in HALT."""
        monkeypatch.setenv("HFT_GATEWAY_HALT_CANCEL", "0")
        policy = GatewayPolicy()
        policy.set_halt()

        intent = _make_intent(intent_type=IntentType.CANCEL, target_order_id="X1")
        allowed, reason = policy.gate(intent, StormGuardState.HALT)

        assert allowed is False
        assert reason == "HALT"

    @pytest.mark.asyncio
    async def test_adapter_dispatches_cancel_with_halt_state(self, adapter):
        """CANCEL orders with HALT storm_guard_state are dispatched by adapter."""
        adapter.running = False
        adapter.live_orders["test_strat:1"] = {"id": "T1"}
        adapter.order_id_map["X1"] = "test_strat:1"

        intent = _make_intent(
            intent_type=IntentType.CANCEL,
            target_order_id="X1",
        )
        cmd = _make_cmd(intent, storm_guard_state=StormGuardState.HALT)

        await adapter._dispatch_to_api(cmd)

        adapter.client.cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_with_halt_increments_metric(self, adapter):
        """Successful CANCEL under HALT state increments order_actions_total."""
        adapter.running = False
        adapter.live_orders["test_strat:1"] = {"id": "T1"}
        adapter.order_id_map["X1"] = "test_strat:1"

        intent = _make_intent(
            intent_type=IntentType.CANCEL,
            target_order_id="X1",
        )
        cmd = _make_cmd(intent, storm_guard_state=StormGuardState.HALT)

        await adapter._dispatch_to_api(cmd)

        adapter.metrics.order_actions_total.labels.assert_called_with(type="cancel")


# ===========================================================================
# 3. place_order returns None — graceful handling
# ===========================================================================


class TestPlaceOrderReturnsNone:
    """When broker place_order returns None (silent failure), adapter must
    handle gracefully: no crash, no live_orders update, no order_id_map update."""

    @pytest.mark.asyncio
    async def test_no_crash(self, adapter):
        """Adapter does not crash when place_order returns None."""
        adapter.client.place_order.return_value = None
        adapter.running = False

        intent = _make_intent(intent_type=IntentType.NEW)
        cmd = _make_cmd(intent)

        # Must not raise
        await adapter._dispatch_to_api(cmd)

    @pytest.mark.asyncio
    async def test_no_live_orders_entry(self, adapter):
        """When place_order returns None, live_orders is not updated."""
        adapter.client.place_order.return_value = None
        adapter.running = False

        intent = _make_intent(intent_type=IntentType.NEW, intent_id=42)
        cmd = _make_cmd(intent)

        await adapter._dispatch_to_api(cmd)

        assert "test_strat:42" not in adapter.live_orders

    @pytest.mark.asyncio
    async def test_no_order_id_map_entry(self, adapter):
        """When place_order returns None, order_id_map is not updated."""
        adapter.client.place_order.return_value = None
        adapter.running = False
        initial_map_size = len(adapter.order_id_map)

        intent = _make_intent(intent_type=IntentType.NEW, intent_id=42)
        cmd = _make_cmd(intent)

        await adapter._dispatch_to_api(cmd)

        assert len(adapter.order_id_map) == initial_map_size

    @pytest.mark.asyncio
    async def test_actions_metric_not_incremented(self, adapter):
        """When place_order returns None, order_actions_total is not incremented."""
        adapter.client.place_order.return_value = None
        adapter.running = False

        intent = _make_intent(intent_type=IntentType.NEW)
        cmd = _make_cmd(intent)

        await adapter._dispatch_to_api(cmd)

        adapter.metrics.order_actions_total.labels.assert_not_called()


# ===========================================================================
# 4. order_id_map updated after successful placement
# ===========================================================================


class TestOrderIdMapUpdated:
    """After successful place_order, internal order ID mapping must be populated."""

    @pytest.mark.asyncio
    async def test_object_attrs_registered(self, adapter):
        """Broker trade object attributes are registered in order_id_map."""
        trade_obj = MagicMock()
        trade_obj.id = "BID-001"
        trade_obj.seq_no = "SEQ-001"
        trade_obj.ord_no = "ORD-001"
        trade_obj.order_id = None
        trade_obj.order = None
        adapter.client.place_order.return_value = trade_obj
        adapter.running = False

        intent = _make_intent(intent_type=IntentType.NEW, intent_id=7)
        cmd = _make_cmd(intent)

        await adapter._dispatch_to_api(cmd)

        assert adapter.order_id_map.get("BID-001") == "test_strat:7"
        assert adapter.order_id_map.get("SEQ-001") == "test_strat:7"
        assert adapter.order_id_map.get("ORD-001") == "test_strat:7"

    @pytest.mark.asyncio
    async def test_dict_trade_ids_registered(self, adapter):
        """Dict-shaped broker trade has its IDs registered in order_id_map."""
        trade_dict = {"id": "D-001", "seq_no": "DS-001", "ord_no": None}
        adapter.client.place_order.return_value = trade_dict
        adapter.running = False

        intent = _make_intent(intent_type=IntentType.NEW, intent_id=8)
        cmd = _make_cmd(intent)

        await adapter._dispatch_to_api(cmd)

        assert adapter.order_id_map.get("D-001") == "test_strat:8"
        assert adapter.order_id_map.get("DS-001") == "test_strat:8"
        # None values must NOT be registered
        assert None not in adapter.order_id_map

    @pytest.mark.asyncio
    async def test_live_orders_stored(self, adapter):
        """After successful placement, live_orders contains the trade object."""
        trade_obj = MagicMock()
        trade_obj.id = "T-99"
        trade_obj.seq_no = None
        trade_obj.ord_no = None
        trade_obj.order_id = None
        trade_obj.order = None
        adapter.client.place_order.return_value = trade_obj
        adapter.running = False

        intent = _make_intent(intent_type=IntentType.NEW, intent_id=99)
        cmd = _make_cmd(intent)

        await adapter._dispatch_to_api(cmd)

        assert "test_strat:99" in adapter.live_orders
        assert adapter.live_orders["test_strat:99"] is trade_obj

    @pytest.mark.asyncio
    async def test_nested_order_ids_registered(self, adapter):
        """Broker trade with nested .order object has nested IDs registered."""
        inner_order = MagicMock()
        inner_order.seq_no = "INNER-SEQ"
        inner_order.ord_no = "INNER-ORD"
        inner_order.order_id = None
        inner_order.id = None

        trade_obj = MagicMock()
        trade_obj.id = "OUTER-ID"
        trade_obj.seq_no = None
        trade_obj.ord_no = None
        trade_obj.order_id = None
        trade_obj.order = inner_order
        adapter.client.place_order.return_value = trade_obj
        adapter.running = False

        intent = _make_intent(intent_type=IntentType.NEW, intent_id=55)
        cmd = _make_cmd(intent)

        await adapter._dispatch_to_api(cmd)

        assert adapter.order_id_map.get("OUTER-ID") == "test_strat:55"
        assert adapter.order_id_map.get("INNER-SEQ") == "test_strat:55"
        assert adapter.order_id_map.get("INNER-ORD") == "test_strat:55"


# ===========================================================================
# 5. load_config failure handling
# ===========================================================================


class TestLoadConfigFailure:
    """Config loading failures should be handled gracefully."""

    def test_missing_config_file_raises(self):
        """Missing config file raises FileNotFoundError at construction time."""
        from hft_platform.order.adapter import OrderAdapter

        q = asyncio.Queue()
        client = MagicMock()

        with pytest.raises(FileNotFoundError):
            OrderAdapter(
                config_path="/nonexistent/path/order.yaml",
                order_queue=q,
                shioaji_client=client,
            )

    def test_empty_config_uses_defaults(self, tmp_path):
        """Empty YAML config file does not crash; adapter uses defaults."""
        from hft_platform.order.adapter import OrderAdapter

        cfg_file = tmp_path / "empty.yaml"
        cfg_file.write_text("")

        q = asyncio.Queue()
        client = MagicMock()
        oa = OrderAdapter(
            config_path=str(cfg_file),
            order_queue=q,
            shioaji_client=client,
        )

        # Should have default rate limiter and circuit breaker
        assert oa.rate_limiter is not None
        assert oa.circuit_breaker is not None

    def test_malformed_yaml_raises(self, tmp_path):
        """Malformed YAML raises an error at construction time."""
        from hft_platform.order.adapter import OrderAdapter

        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("rate_limits:\n  - [\ninvalid yaml")

        q = asyncio.Queue()
        client = MagicMock()

        with pytest.raises(Exception):
            OrderAdapter(
                config_path=str(cfg_file),
                order_queue=q,
                shioaji_client=client,
            )

    def test_partial_config_preserves_defaults(self, tmp_path):
        """Config with only some keys preserves default values for unset fields."""
        from hft_platform.order.adapter import OrderAdapter

        cfg_file = tmp_path / "partial.yaml"
        cfg_file.write_text("rate_limits:\n  shioaji_soft_cap: 100\n")

        q = asyncio.Queue()
        client = MagicMock()
        oa = OrderAdapter(
            config_path=str(cfg_file),
            order_queue=q,
            shioaji_client=client,
        )

        assert oa.rate_limiter.soft_cap == 100
        assert oa.rate_limiter.hard_cap == 250  # default preserved
