"""Tests for OrderAdapter: _dispatch_to_api, _register_broker_ids, drain_and_cancel, load_config."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter


class _StubCodec:
    def encode_side(self, side: Any) -> str:
        return "Buy"

    def encode_tif(self, tif: Any) -> str:
        return "ROD"

    def encode_price_type(self, price_type: Any) -> str:
        return "LMT"


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
    return OrderIntent(
        intent_id=intent_id,
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
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=0,
    )


def _make_adapter(tmp_config: str, client=None):
    if client is None:
        client = _make_client()
    q: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter(config_path=tmp_config, order_queue=q, broker_client=client, broker_codec=_StubCodec())
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
    trade.seqno = None
    trade.ord_no = None
    trade.ordno = None
    trade.order_id = None
    trade.id = None
    trade.order = None
    trade.status = None
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
async def test_register_broker_ids_with_shioaji_live_field_names(tmp_config):
    """Shioaji live futures uses ordno/seqno rather than ord_no/seq_no."""
    adapter = _make_adapter(tmp_config)

    order = MagicMock()
    order.seqno = "LIVE_SEQ"
    order.ordno = "LIVE_ORD"
    order.id = "LIVE_ID"

    trade = MagicMock()
    trade.seqno = "LIVE_SEQ"
    trade.ordno = "LIVE_ORD"
    trade.id = "LIVE_ID"
    trade.order = order

    await adapter._register_broker_ids("s1:live", trade)

    assert adapter.order_id_map["LIVE_SEQ"] == "s1:live"
    assert adapter.order_id_map["LIVE_ORD"] == "s1:live"
    assert adapter.order_id_map["LIVE_ID"] == "s1:live"


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
    """Eviction removes oldest 10% of non-live entries when map reaches max size."""
    adapter = _make_adapter(tmp_config)
    adapter._order_id_map_max_size = 100
    for i in range(100):
        adapter.order_id_map[f"id_{i:04d}"] = f"key_{i}"
    trade = MagicMock()
    trade.seq_no = "TRIGGER"
    trade.seqno = None
    trade.ord_no = None
    trade.ordno = None
    trade.order_id = None
    trade.id = None
    trade.order = None
    trade.status = None
    await adapter._register_broker_ids("s1:trigger", trade)
    # Should evict 10 oldest non-live entries + add 1 new = 91
    assert len(adapter.order_id_map) == 91
    assert "TRIGGER" in adapter.order_id_map
    for i in range(10):
        assert f"id_{i:04d}" not in adapter.order_id_map
    assert "id_0010" in adapter.order_id_map


@pytest.mark.asyncio
async def test_order_id_map_eviction_skips_live_orders(tmp_config):
    """M6: Eviction must not remove entries whose order_key is in live_orders."""
    adapter = _make_adapter(tmp_config)
    adapter._order_id_map_max_size = 20
    # Seed 20 entries, make the first 5 point to live order keys
    for i in range(20):
        adapter.order_id_map[f"id_{i:02d}"] = f"key_{i}"
    # Mark first 5 as live
    for i in range(5):
        adapter.live_orders[f"key_{i}"] = MagicMock()

    trade = MagicMock()
    trade.seq_no = "NEW"
    trade.seqno = None
    trade.ord_no = None
    trade.ordno = None
    trade.order_id = None
    trade.id = None
    trade.order = None
    trade.status = None
    await adapter._register_broker_ids("s1:new", trade)

    # Live entries (key_0..key_4) must survive eviction
    for i in range(5):
        assert f"id_{i:02d}" in adapter.order_id_map, f"Live entry id_{i:02d} should NOT be evicted"
    assert "NEW" in adapter.order_id_map


# ---------------------------------------------------------------------------
# Coalesce window bypass for urgent intents (H3 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_skips_coalesce_window(tmp_config):
    """CANCEL intents should bypass the coalesce window for minimal latency."""
    import time

    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    adapter._api_coalesce_window_s = 0.5  # 500ms window — would be very noticeable

    # Seed a live order so cancel dispatch finds its target
    adapter.live_orders["strat1:ORD-TARGET"] = MagicMock()

    cancel_intent = _make_intent(
        intent_type=IntentType.CANCEL,
        target_order_id="ORD-TARGET",
    )
    cancel_cmd = _make_cmd(intent=cancel_intent)

    await adapter._api_queue.put(cancel_cmd)

    # Run worker briefly — it should dispatch without waiting 500ms
    adapter.running = True
    t0 = time.monotonic()
    worker = asyncio.create_task(adapter._api_worker())
    # Give enough time for one iteration but NOT 500ms
    await asyncio.sleep(0.05)
    adapter.running = False
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    elapsed = time.monotonic() - t0

    # Should have dispatched within 50ms, not waited 500ms coalesce
    assert elapsed < 0.2, f"Took {elapsed:.3f}s — coalesce window was NOT bypassed"
    client.cancel_order.assert_called_once()


@pytest.mark.asyncio
async def test_force_flat_skips_coalesce_window(tmp_config):
    """FORCE_FLAT intents should bypass the coalesce window (timing check)."""
    import time

    client = _make_client()
    adapter = _make_adapter(tmp_config, client)
    adapter._api_coalesce_window_s = 0.5

    flat_intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
    flat_cmd = _make_cmd(intent=flat_intent)

    dispatched = []
    original_dispatch = adapter._dispatch_to_api

    async def _track_dispatch(cmd):
        dispatched.append(time.monotonic())
        return await original_dispatch(cmd)

    adapter._dispatch_to_api = _track_dispatch
    await adapter._api_queue.put(flat_cmd)

    adapter.running = True
    t0 = time.monotonic()
    worker = asyncio.create_task(adapter._api_worker())
    await asyncio.sleep(0.05)
    adapter.running = False
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    # Dispatch should have been reached within 50ms, not after 500ms coalesce
    assert len(dispatched) == 1, "Expected exactly one dispatch call"
    assert dispatched[0] - t0 < 0.2, f"Dispatch took {dispatched[0] - t0:.3f}s — coalesce NOT bypassed"


# ---------------------------------------------------------------------------
# StormGuard HALT-skip in _api_worker: DLQ entry + dedicated metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_worker_halt_skip_sends_dlq_and_metric(tmp_config):
    """When StormGuard transitions to HALT between risk-check and dispatch,
    the _api_worker must (a) increment order_halt_skip_total, (b) add the
    order to the DLQ with reason STORMGUARD_HALT, and (c) NOT call dispatch.
    """
    from unittest.mock import AsyncMock, MagicMock

    from hft_platform.order.deadletter import RejectionReason

    client = _make_client()
    adapter = _make_adapter(tmp_config, client)

    # Inject a mock DLQ so we can assert on it
    dlq_mock = AsyncMock()
    adapter._dlq = dlq_mock

    # Inject a mock metrics object so we can assert counter increments
    metrics_mock = MagicMock()
    metrics_mock.order_halt_skip_total = MagicMock()
    metrics_mock.order_reject_total = MagicMock()
    adapter.metrics = metrics_mock

    # Set StormGuard to HALT
    sg = MagicMock()
    sg.state = StormGuardState.HALT
    sg.is_halt_exempt.return_value = False
    adapter._storm_guard = sg

    # Disable coalesce window to keep the test fast
    adapter._api_coalesce_window_s = 0.0

    # Build a NEW intent (non-exempt) and put it into the _api_queue
    intent = _make_intent(intent_type=IntentType.NEW, strategy_id="strat_halt", symbol="0050")
    cmd = _make_cmd(intent=intent)
    await adapter._api_queue.put(cmd)

    # Patch _dispatch_to_api to confirm it is NOT called
    dispatch_mock = AsyncMock()
    adapter._dispatch_to_api = dispatch_mock

    # Run the worker for one cycle then cancel
    adapter.running = True
    worker = asyncio.create_task(adapter._api_worker())
    await asyncio.sleep(0.05)
    adapter.running = False
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    # Dedicated metric incremented
    metrics_mock.order_halt_skip_total.labels.assert_called_once_with(strategy_id="strat_halt")
    metrics_mock.order_halt_skip_total.labels.return_value.inc.assert_called_once()
    # Backward-compat reject metric also incremented
    metrics_mock.order_reject_total.inc.assert_called_once()
    # DLQ received the entry
    dlq_mock.add.assert_awaited_once()
    call_kwargs = dlq_mock.add.call_args[1]
    assert call_kwargs["reason"] == RejectionReason.STORMGUARD_HALT
    assert "STORMGUARD_HALT_SKIP" in call_kwargs["error_message"]
    assert call_kwargs["strategy_id"] == "strat_halt"
    assert call_kwargs["symbol"] == "0050"
    # Broker dispatch must NOT be called
    dispatch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_worker_halt_skip_exempt_cancel_still_dispatched(tmp_config):
    """CANCEL intents are exempt from HALT-skip: they must reach _dispatch_to_api
    even when StormGuard is in HALT."""
    from unittest.mock import AsyncMock, MagicMock

    client = _make_client()
    adapter = _make_adapter(tmp_config, client)

    metrics_mock = MagicMock()
    metrics_mock.order_halt_skip_total = MagicMock()
    metrics_mock.order_reject_total = MagicMock()
    adapter.metrics = metrics_mock

    sg = MagicMock()
    sg.state = StormGuardState.HALT
    adapter._storm_guard = sg
    adapter._api_coalesce_window_s = 0.0

    intent = _make_intent(intent_type=IntentType.CANCEL, strategy_id="strat_cancel")
    intent.target_order_id = "strat_cancel:1"
    cmd = _make_cmd(intent=intent)
    await adapter._api_queue.put(cmd)

    dispatch_mock = AsyncMock()
    adapter._dispatch_to_api = dispatch_mock

    adapter.running = True
    worker = asyncio.create_task(adapter._api_worker())
    await asyncio.sleep(0.05)
    adapter.running = False
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    # HALT-skip metric must NOT fire for exempt intents
    metrics_mock.order_halt_skip_total.inc.assert_not_called()
    # Dispatch must have been called for the CANCEL
    dispatch_mock.assert_awaited_once_with(cmd)
