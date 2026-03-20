"""Tests for OrderAdapter: _dispatch_to_api, _register_broker_ids, drain_and_cancel, load_config."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
)
from hft_platform.order.adapter import OrderAdapter
from tests.factories.intents import make_order_command, make_order_intent


@pytest.fixture
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg)


@pytest.fixture(autouse=True)
def mock_deps():
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata") as ms,
        patch("hft_platform.order.adapter.PriceCodec") as mp,
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        mm.get.return_value = MagicMock()
        ml.get.return_value = MagicMock()
        md.return_value = AsyncMock()
        mp_inst = MagicMock()
        mp_inst.descale.return_value = 500.0
        mp.return_value = mp_inst
        meta_inst = MagicMock()
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield


def _make_client():
    """Return a mock broker client with the required methods."""
    client = MagicMock()
    client.place_order = MagicMock(
        return_value=MagicMock(seq_no="S1", ord_no="O1", order_id="ID1", id="X1", order=None)
    )
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TSE")
    return client


def _make_intent(
    intent_type: IntentType = IntentType.NEW,
    *,
    intent_id: int = 1,
    strategy_id: str = "strat1",
    symbol: str = "2330",
    side: Side = Side.BUY,
    price: int = 500_0000,
    qty: int = 1,
    target_order_id: str | None = None,
) -> OrderIntent:
    return make_order_intent(
        intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,
        qty=qty,
        target_order_id=target_order_id,
    )


def _make_cmd(intent: OrderIntent | None = None, **kw) -> OrderCommand:
    if intent is None:
        intent = _make_intent(**kw)
    return make_order_command(intent=intent)


def _make_adapter(tmp_config: str, client=None):
    if client is None:
        client = _make_client()
    q: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter(config_path=tmp_config, order_queue=q, shioaji_client=client)
    return adapter


@pytest.mark.asyncio
async def test_dispatch_new_places_order(tmp_config):
    """_dispatch_to_api NEW calls client.place_order with descaled price."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    cmd = _make_cmd(intent_type=IntentType.NEW)
    await adapter._dispatch_to_api(cmd)
    client.place_order.assert_called_once()
    call_kwargs = client.place_order.call_args
    assert call_kwargs.kwargs["price"] == 500.0
    assert "action" in call_kwargs.kwargs
    assert "exchange" in call_kwargs.kwargs


@pytest.mark.asyncio
async def test_dispatch_new_stores_in_live_orders(tmp_config):
    """_dispatch_to_api NEW stores the trade object in live_orders."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=42)
    cmd = _make_cmd(intent)
    await adapter._dispatch_to_api(cmd)
    assert "s1:42" in adapter.live_orders


@pytest.mark.asyncio
async def test_dispatch_cancel_calls_cancel_order(tmp_config):
    """_dispatch_to_api CANCEL calls client.cancel_order with the target trade."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    fake_trade = MagicMock()
    adapter.live_orders["s1:100"] = fake_trade
    intent = _make_intent(IntentType.CANCEL, strategy_id="s1", target_order_id="100")
    cmd = _make_cmd(intent)
    await adapter._dispatch_to_api(cmd)
    client.cancel_order.assert_called_once()
    assert client.cancel_order.call_args[0][0] is fake_trade


@pytest.mark.asyncio
async def test_dispatch_cancel_missing_target_logs_warning(tmp_config):
    """_dispatch_to_api CANCEL with missing target does not call cancel_order."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    intent = _make_intent(IntentType.CANCEL, strategy_id="s1", target_order_id="missing_999")
    cmd = _make_cmd(intent)
    await adapter._dispatch_to_api(cmd)
    client.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_amend_calls_update_order(tmp_config):
    """_dispatch_to_api AMEND calls client.update_order with descaled price."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    fake_trade = MagicMock()
    adapter.live_orders["s1:200"] = fake_trade
    intent = _make_intent(IntentType.AMEND, strategy_id="s1", price=600_0000, target_order_id="200")
    cmd = _make_cmd(intent)
    await adapter._dispatch_to_api(cmd)
    client.update_order.assert_called_once()
    call_kwargs = client.update_order.call_args
    assert call_kwargs.kwargs["price"] == 500.0


@pytest.mark.asyncio
async def test_register_broker_ids_maps_ids(tmp_config):
    """_register_broker_ids extracts seq_no, ord_no, order_id, id and maps them."""
    adapter = _make_adapter(tmp_config)
    trade = MagicMock()
    trade.seq_no = "SEQ1"
    trade.ord_no = "ORD1"
    trade.order_id = "OID1"
    trade.id = "XID1"
    trade.order = None
    await adapter._register_broker_ids("s1:1", trade)
    assert adapter.order_id_map["SEQ1"] == "s1:1"
    assert adapter.order_id_map["ORD1"] == "s1:1"
    assert adapter.order_id_map["OID1"] == "s1:1"
    assert adapter.order_id_map["XID1"] == "s1:1"


@pytest.mark.asyncio
async def test_register_broker_ids_eviction_at_max_size(tmp_config):
    """_register_broker_ids evicts oldest 10% when order_id_map is at max size."""
    adapter = _make_adapter(tmp_config)
    adapter._order_id_map_max_size = 20
    for i in range(20):
        adapter.order_id_map[f"old_{i}"] = f"key_{i}"
    trade = MagicMock()
    trade.seq_no = "NEW_SEQ"
    trade.ord_no = None
    trade.order_id = None
    trade.id = None
    trade.order = None
    await adapter._register_broker_ids("s1:new", trade)
    assert len(adapter.order_id_map) == 19
    assert "NEW_SEQ" in adapter.order_id_map
    assert "old_0" not in adapter.order_id_map
    assert "old_1" not in adapter.order_id_map


@pytest.mark.asyncio
async def test_register_broker_ids_with_dict_trade(tmp_config):
    """_register_broker_ids handles dict-based trade objects."""
    adapter = _make_adapter(tmp_config)
    trade = {
        "seq_no": "DSEQ",
        "ord_no": "DORD",
        "order_id": "",
        "id": "DID",
        "order": {"seq_no": "INNER_SEQ", "ord_no": "", "order_id": "", "id": ""},
    }
    await adapter._register_broker_ids("s1:d1", trade)
    assert adapter.order_id_map["DSEQ"] == "s1:d1"
    assert adapter.order_id_map["DORD"] == "s1:d1"
    assert adapter.order_id_map["DID"] == "s1:d1"
    assert adapter.order_id_map["INNER_SEQ"] == "s1:d1"
    assert "" not in adapter.order_id_map


@pytest.mark.asyncio
async def test_drain_and_cancel_drains_queue_and_cancels(tmp_config):
    """drain_and_cancel drains order_queue and cancels all live orders."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    adapter.order_queue.put_nowait("dummy_cmd_1")
    adapter.order_queue.put_nowait("dummy_cmd_2")
    adapter.live_orders["s1:1"] = MagicMock()
    adapter.live_orders["s1:2"] = MagicMock()
    count = await adapter.drain_and_cancel(timeout_s=1.0)
    assert count == 2
    assert adapter.order_queue.empty()
    assert client.cancel_order.call_count == 2


@pytest.mark.asyncio
async def test_drain_and_cancel_handles_cancel_timeout(tmp_config):
    """drain_and_cancel handles timeout during cancel gracefully."""
    client = _make_client()
    client.cancel_order = MagicMock(side_effect=Exception("timeout"))
    adapter = _make_adapter(tmp_config, client)
    adapter.live_orders["s1:1"] = MagicMock()
    count = await adapter.drain_and_cancel(timeout_s=1.0)
    assert count == 0


@pytest.mark.asyncio
async def test_drain_and_cancel_with_empty_queue(tmp_config):
    """drain_and_cancel with no pending orders returns 0."""
    adapter = _make_adapter(tmp_config)
    count = await adapter.drain_and_cancel(timeout_s=1.0)
    assert count == 0
    assert adapter.order_queue.empty()


@pytest.mark.asyncio
async def test_load_config_updates_rate_limiter(tmp_config):
    """load_config reads YAML and updates rate_limiter settings."""
    adapter = _make_adapter(tmp_config)
    assert adapter.rate_limiter.soft_cap == 180
    assert adapter.rate_limiter.hard_cap == 250
    assert adapter.rate_limiter.window_s == 10


@pytest.mark.asyncio
async def test_load_config_updates_circuit_breaker(tmp_config):
    """load_config reads YAML and updates circuit_breaker settings."""
    adapter = _make_adapter(tmp_config)
    assert adapter.circuit_breaker.threshold == 5
    assert adapter.circuit_breaker.timeout_s == 60


@pytest.mark.asyncio
async def test_order_id_map_eviction_removes_oldest_10_percent(tmp_config):
    """Eviction removes oldest 10% of entries when map reaches max size."""
    adapter = _make_adapter(tmp_config)
    adapter._order_id_map_max_size = 100
    for i in range(100):
        adapter.order_id_map[f"id_{i:04d}"] = f"key_{i}"
    trade = MagicMock()
    trade.seq_no = "TRIGGER"
    trade.ord_no = None
    trade.order_id = None
    trade.id = None
    trade.order = None
    await adapter._register_broker_ids("s1:trigger", trade)
    assert len(adapter.order_id_map) == 91
    assert "TRIGGER" in adapter.order_id_map
    for i in range(10):
        assert f"id_{i:04d}" not in adapter.order_id_map
    assert "id_0010" in adapter.order_id_map
