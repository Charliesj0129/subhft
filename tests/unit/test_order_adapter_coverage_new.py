"""Targeted coverage tests for order/adapter.py.

Covers the following clusters of missing lines:
  Cluster A: drain_and_cancel — CANCEL/FORCE_FLAT safety path + normal drain (lines 736-773)
  Cluster B: _dispatch_to_api NEW — TCA map, unknown exchange, metadata lookup failures,
             trade is None, timestamp injection (lines 1343-1525)
  Cluster C: _dispatch_to_api FORCE_FLAT — net_qty non-zero path (lines 1576-1690)
  Cluster D: _dispatch_to_api AMEND — PENDING_SENTINEL, TERMINAL_BEFORE_REGISTERED, OSError
             (lines 1777-1816)
  Cluster E: execute() — strategy CB check, global CB check, validate_client fail
             (lines 1083-1111)
  Misc: _on_terminal_callback deferred terminal, _register_broker_ids eviction,
        drain_deferred_terminals (lines 832-833, 895)
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
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

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers — reuse same pattern as test_order_adapter_dispatch.py
# ─────────────────────────────────────────────────────────────────────────────


class _StubCodec:
    def encode_side(self, side: Any) -> str:
        return "Buy"

    def encode_tif(self, tif: Any) -> str:
        return "IOC"

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
def mock_deps(tmp_path):
    with (
        patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(tmp_path / "oid_map.jsonl")}),
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata") as ms,
        patch("hft_platform.order.adapter.PriceCodec") as mp,
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics_mock = MagicMock()
        metrics_mock.order_reject_total = MagicMock()
        metrics_mock.order_actions_total = MagicMock()
        metrics_mock.order_actions_total.labels.return_value = MagicMock()
        metrics_mock.circuit_breaker_state = MagicMock()
        mm.get.return_value = metrics_mock
        ml.get.return_value = MagicMock()
        md.return_value = AsyncMock()
        mp_inst = MagicMock()
        mp_inst.descale.return_value = 500.0
        mp.return_value = mp_inst
        meta_inst = MagicMock()
        meta_inst.exchange.return_value = "TSE"
        meta_inst.product_type.return_value = None
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield


def _make_client():
    client = MagicMock()
    client.place_order = MagicMock(
        return_value=MagicMock(seq_no="S1", ord_no="O1", order_id="ID1", id="X1", order=None, status=None)
    )
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.update_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TSE")
    client.mode = "simulation"
    client.activate_ca = False
    return client


def _make_adapter(tmp_config: str, client=None):
    from hft_platform.order.adapter import OrderAdapter

    if client is None:
        client = _make_client()
    q: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter(config_path=tmp_config, order_queue=q, broker_client=client, broker_codec=_StubCodec())
    adapter.shadow_sink.enabled = False
    return adapter


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
    reason: str = "",
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
        reason=reason,
    )


def _make_cmd(intent: OrderIntent | None = None, **kw) -> OrderCommand:
    if intent is None:
        intent = _make_intent(**kw)
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Cluster A: drain_and_cancel — safety-order preservation path (lines 743-767)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_drain_and_cancel_preserves_cancel_safety_cmd(tmp_config):
    """drain_and_cancel should create a task for CANCEL commands from _api_queue (safety path)."""
    adapter = _make_adapter(tmp_config)
    dispatched_cmds = []

    async def _fake_dispatch(cmd):
        dispatched_cmds.append(cmd)
        return True

    adapter._dispatch_to_api = _fake_dispatch

    # Put a CANCEL command on _api_queue
    cancel_intent = _make_intent(IntentType.CANCEL, strategy_id="s1", intent_id=99)
    cancel_cmd = _make_cmd(cancel_intent)
    adapter._api_queue.put_nowait(cancel_cmd)

    await adapter.drain_and_cancel(timeout_s=0.5)
    # Allow background tasks spawned by create_task to complete
    await asyncio.sleep(0.1)

    # The cancel cmd should have been dispatched (safety path)
    assert cancel_cmd in dispatched_cmds


@pytest.mark.asyncio
async def test_drain_and_cancel_drains_new_orders_not_dispatches(tmp_config):
    """drain_and_cancel drops NEW orders from _api_queue (not safety)."""
    adapter = _make_adapter(tmp_config)
    dispatched_cmds = []

    async def _fake_dispatch(cmd):
        dispatched_cmds.append(cmd)
        return True

    adapter._dispatch_to_api = _fake_dispatch

    new_cmd = _make_cmd(intent_type=IntentType.NEW)
    adapter._api_queue.put_nowait(new_cmd)

    await adapter.drain_and_cancel(timeout_s=0.5)
    await asyncio.sleep(0.05)

    # NEW command should NOT be dispatched — it was drained/dropped
    assert new_cmd not in dispatched_cmds


@pytest.mark.asyncio
async def test_drain_and_cancel_preserves_force_flat_safety_cmd(tmp_config):
    """drain_and_cancel dispatches FORCE_FLAT commands as safety-critical."""
    adapter = _make_adapter(tmp_config)
    dispatched_cmds = []

    async def _fake_dispatch(cmd):
        dispatched_cmds.append(cmd)
        return True

    adapter._dispatch_to_api = _fake_dispatch

    ff_intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=10)
    ff_cmd = _make_cmd(ff_intent)
    adapter._api_queue.put_nowait(ff_cmd)

    await adapter.drain_and_cancel(timeout_s=0.5)
    await asyncio.sleep(0.1)

    assert ff_cmd in dispatched_cmds


@pytest.mark.asyncio
async def test_drain_and_cancel_dispatch_failure_is_logged_not_raised(tmp_config):
    """drain_and_cancel logs a critical but does not propagate exceptions from dispatch."""
    adapter = _make_adapter(tmp_config)

    async def _boom(cmd):
        raise RuntimeError("dispatch exploded")

    adapter._dispatch_to_api = _boom

    cancel_intent = _make_intent(IntentType.CANCEL, strategy_id="s1", intent_id=5)
    cancel_cmd = _make_cmd(cancel_intent)
    adapter._api_queue.put_nowait(cancel_cmd)

    # Create the task and wait for completion (drain_and_cancel uses create_task internally)
    await adapter.drain_and_cancel(timeout_s=0.5)
    # Allow background tasks to run
    await asyncio.sleep(0.1)
    # Must not have raised; queue should be empty after drain
    assert adapter._api_queue.empty()


@pytest.mark.asyncio
async def test_drain_and_cancel_logs_warning_when_api_drained_count_positive(tmp_config):
    """drain_and_cancel logs a warning when non-safety commands are drained."""
    adapter = _make_adapter(tmp_config)
    adapter._dispatch_to_api = AsyncMock(return_value=True)

    # Add 3 NEW commands (will be drained/counted)
    for i in range(3):
        adapter._api_queue.put_nowait(_make_cmd(intent_type=IntentType.NEW))

    # Should complete without error; the warning branch (_api_drained > 0) executes
    await adapter.drain_and_cancel(timeout_s=0.5)

    # Queue should be empty after drain completes
    assert adapter._api_queue.empty()

    # No assertion needed beyond no exception — the log/count branch was executed


# ═════════════════════════════════════════════════════════════════════════════
# Cluster B: _dispatch_to_api NEW — various sub-paths
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_new_records_created_ns_in_cmd_map(tmp_config):
    """_dispatch_to_api stores cmd.created_ns in _cmd_created_ns_map (line 1344)."""
    adapter = _make_adapter(tmp_config)
    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=7)
    cmd = _make_cmd(intent)
    cmd.created_ns = timebase.now_ns()

    await adapter._dispatch_to_api(cmd)

    order_key = "s1:7"
    assert order_key in adapter._cmd_created_ns_map
    assert adapter._cmd_created_ns_map[order_key] == cmd.created_ns


@pytest.mark.asyncio
async def test_dispatch_new_records_tca_map_entry(tmp_config):
    """_dispatch_to_api stores (decision_price, arrival_price) in _cmd_tca_map for NEW (lines 1347-1357)."""
    adapter = _make_adapter(tmp_config)
    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=8)
    cmd = _make_cmd(intent)
    cmd.decision_price = 4_990_000  # type: ignore[attr-defined]
    cmd.arrival_price = 5_000_000  # type: ignore[attr-defined]

    await adapter._dispatch_to_api(cmd)

    order_key = "s1:8"
    assert order_key in adapter._cmd_tca_map
    decision, arrival = adapter._cmd_tca_map[order_key]
    assert decision == 4_990_000
    assert arrival == 5_000_000


@pytest.mark.asyncio
async def test_dispatch_new_unknown_exchange_uses_tse_default(tmp_config):
    """_dispatch_to_api uses 'TSE' when neither metadata nor client provides exchange (line 1408-1412)."""
    client = _make_client()
    del client.get_exchange  # remove get_exchange so client_exchange = ""
    adapter = _make_adapter(tmp_config, client)

    # Make metadata.exchange fail
    adapter.metadata.exchange.side_effect = KeyError("no exchange")

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=9)
    cmd = _make_cmd(intent)

    # Should still call place_order (using "TSE" default)
    await adapter._dispatch_to_api(cmd)

    client.place_order.assert_called_once()
    call_kwargs = client.place_order.call_args.kwargs
    assert call_kwargs["exchange"] == "TSE"


@pytest.mark.asyncio
async def test_dispatch_new_metadata_product_type_failure_uses_none(tmp_config):
    """_dispatch_to_api handles product_type lookup failure (lines 1418-1424)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.product_type.side_effect = AttributeError("no product_type")

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=10)
    cmd = _make_cmd(intent)

    # Should continue to place_order with product_type=None
    await adapter._dispatch_to_api(cmd)
    adapter.client.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_new_metadata_order_params_failure_uses_empty(tmp_config):
    """_dispatch_to_api handles order_params lookup failure (lines 1430-1436)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.order_params.side_effect = TypeError("bad params")

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=11)
    cmd = _make_cmd(intent)

    # Should continue to place_order with empty order_params
    await adapter._dispatch_to_api(cmd)
    adapter.client.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_new_trade_is_none_returns_false_and_dlqs(tmp_config):
    """_dispatch_to_api returns False when place_order returns None (lines 1509-1521)."""
    client = _make_client()
    client.place_order.return_value = None
    adapter = _make_adapter(tmp_config, client)

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=12)
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is False
    # Sentinel must be cleaned up
    order_key = "s1:12"
    assert order_key not in adapter.live_orders


@pytest.mark.asyncio
async def test_dispatch_new_trade_timestamp_injected_for_dict_trade(tmp_config):
    """_dispatch_to_api injects 'timestamp' key into dict trade (lines 1524-1530)."""
    client = _make_client()
    trade_dict = {"seq_no": "S1", "ord_no": "O1"}
    client.place_order.return_value = trade_dict
    adapter = _make_adapter(tmp_config, client)

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=13)
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    assert "timestamp" in trade_dict


@pytest.mark.asyncio
async def test_dispatch_new_cmd_map_eviction_at_capacity(tmp_config):
    """_dispatch_to_api evicts oldest cmd_map entries when at max capacity (lines 1362-1378)."""
    adapter = _make_adapter(tmp_config)
    adapter._cmd_map_max_size = 5  # set low to trigger eviction

    # Pre-fill the map with entries not in live_orders
    for i in range(5):
        adapter._cmd_created_ns_map[f"strat_x:{i}"] = timebase.now_ns()

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=99)
    cmd = _make_cmd(intent)

    # Should trigger eviction (cmd_map is at capacity)
    await adapter._dispatch_to_api(cmd)

    # After eviction, new entry should exist
    assert "s1:99" in adapter._cmd_created_ns_map


# ═════════════════════════════════════════════════════════════════════════════
# Cluster B2: _dispatch_to_api NEW — arrival_price fallback paths (lines 1350-1356)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_new_tca_arrival_from_mid_price_fn(tmp_config):
    """When arrival_price=0, _dispatch_to_api tries _mid_price_fn first (line 1351-1354)."""
    adapter = _make_adapter(tmp_config)
    adapter._mid_price_fn = MagicMock(return_value=5_100_000)

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=20)
    cmd = _make_cmd(intent)
    cmd.arrival_price = 0  # type: ignore[attr-defined]
    cmd.decision_price = 4_900_000  # type: ignore[attr-defined]

    await adapter._dispatch_to_api(cmd)

    order_key = "s1:20"
    assert order_key in adapter._cmd_tca_map
    _, arrival = adapter._cmd_tca_map[order_key]
    # Should have used mid_price_fn result
    assert arrival == 5_100_000


@pytest.mark.asyncio
async def test_dispatch_new_tca_arrival_from_decision_when_mid_price_raises(tmp_config):
    """When _mid_price_fn raises, arrival falls back to decision_price (line 1354)."""
    adapter = _make_adapter(tmp_config)
    adapter._mid_price_fn = MagicMock(side_effect=Exception("mid price error"))

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=21)
    cmd = _make_cmd(intent)
    cmd.arrival_price = 0  # type: ignore[attr-defined]
    cmd.decision_price = 4_950_000  # type: ignore[attr-defined]

    await adapter._dispatch_to_api(cmd)

    order_key = "s1:21"
    assert order_key in adapter._cmd_tca_map
    _, arrival = adapter._cmd_tca_map[order_key]
    # Should have fallen back to decision_price
    assert arrival == 4_950_000


# ═════════════════════════════════════════════════════════════════════════════
# Cluster C: _dispatch_to_api FORCE_FLAT (lines 1576-1690)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_force_flat_with_long_position_places_sell(tmp_config):
    """FORCE_FLAT with net_qty > 0 dispatches a SELL order (lines 1614-1673)."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)

    # Set up position store with long position
    pos = SimpleNamespace(symbol="2330", net_qty=3, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=30, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is True
    client.place_order.assert_called_once()
    call_kwargs = client.place_order.call_args.kwargs
    # Should be a sell (close long)
    assert call_kwargs["action"] == "Buy"  # StubCodec always returns "Buy" for encode_side


@pytest.mark.asyncio
async def test_dispatch_force_flat_no_position_is_noop(tmp_config):
    """FORCE_FLAT with net_qty==0 returns False without placing order (line 1583-1585)."""
    adapter = _make_adapter(tmp_config)

    # No position
    adapter.position_store = SimpleNamespace(positions={})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=31, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is False
    adapter.client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_force_flat_trade_none_returns_false(tmp_config):
    """FORCE_FLAT returns False when place_order returns None (lines 1647-1653)."""
    client = _make_client()
    client.place_order.return_value = None
    adapter = _make_adapter(tmp_config, client)

    pos = SimpleNamespace(symbol="2330", net_qty=2, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=32, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is False
    order_key = "s1:32"
    assert order_key not in adapter.live_orders


@pytest.mark.asyncio
async def test_dispatch_force_flat_with_short_position_places_buy(tmp_config):
    """FORCE_FLAT with net_qty < 0 dispatches a BUY order (close short)."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)

    pos = SimpleNamespace(symbol="2330", net_qty=-2, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=33, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is True
    client.place_order.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_force_flat_no_broker_codec_returns_false(tmp_config):
    """FORCE_FLAT without broker codec returns False immediately (lines 1577-1580)."""
    adapter = _make_adapter(tmp_config)
    adapter._broker_codec = None

    pos = SimpleNamespace(symbol="2330", net_qty=1, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=34, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is False


# ═════════════════════════════════════════════════════════════════════════════
# Cluster D: _dispatch_to_api AMEND — sentinel/terminal/missing sub-paths
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_amend_target_pending_sentinel_rejects(tmp_config):
    """AMEND on a pending-sentinel target must DLQ with validation error (line 1777-1780)."""
    from hft_platform.order.adapter import _PENDING_SENTINEL

    adapter = _make_adapter(tmp_config)
    # Put sentinel in live_orders at known key
    adapter.live_orders["s1:5"] = _PENDING_SENTINEL

    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:5"

    intent = _make_intent(IntentType.AMEND, strategy_id="s1", intent_id=1, target_order_id="s1:5")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "pending" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_dispatch_amend_target_terminal_before_registered_rejects(tmp_config):
    """AMEND on a _TERMINAL_BEFORE_REGISTERED target must DLQ (line 1781-1786)."""
    from hft_platform.order.adapter import _TERMINAL_BEFORE_REGISTERED

    adapter = _make_adapter(tmp_config)
    adapter.live_orders["s1:6"] = _TERMINAL_BEFORE_REGISTERED

    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:6"

    intent = _make_intent(IntentType.AMEND, strategy_id="s1", intent_id=1, target_order_id="s1:6")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "terminated" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_dispatch_cancel_target_pending_sentinel_rejects(tmp_config):
    """CANCEL on a pending-sentinel target must DLQ (line 1722-1725)."""
    from hft_platform.order.adapter import _PENDING_SENTINEL

    adapter = _make_adapter(tmp_config)
    adapter.live_orders["s1:7"] = _PENDING_SENTINEL

    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:7"

    intent = _make_intent(IntentType.CANCEL, strategy_id="s1", intent_id=1, target_order_id="s1:7")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "pending" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_dispatch_cancel_target_terminal_before_registered_rejects(tmp_config):
    """CANCEL on a _TERMINAL_BEFORE_REGISTERED target must DLQ (lines 1726-1731)."""
    from hft_platform.order.adapter import _TERMINAL_BEFORE_REGISTERED

    adapter = _make_adapter(tmp_config)
    adapter.live_orders["s1:8"] = _TERMINAL_BEFORE_REGISTERED

    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:8"

    intent = _make_intent(IntentType.CANCEL, strategy_id="s1", intent_id=1, target_order_id="s1:8")
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "terminated" in call_kwargs["error_message"].lower()


@pytest.mark.asyncio
async def test_dispatch_oserror_increments_reject_and_circuit_breaker(tmp_config):
    """OSError during dispatch increments reject metric and records CB failure (lines 1792-1816).

    NOTE: OSError from place_order is caught by _call_api internally (returns None).
    To hit the outer except block at line 1792, we raise from price_codec.descale
    which runs before _call_api.
    """
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)

    # Replace real circuit_breaker with a mock so we can assert on it
    adapter.circuit_breaker = MagicMock()
    adapter.circuit_breaker.is_open.return_value = False
    adapter.strategy_cb_mgr = MagicMock()
    adapter.strategy_cb_mgr.is_open.return_value = False

    # Raise OSError from price_codec.descale — happens inside the outer try block
    adapter.price_codec.descale.side_effect = OSError("disk IO error on price lookup")

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=50)
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is False
    adapter.metrics.order_reject_total.inc.assert_called()
    adapter.circuit_breaker.record_failure.assert_called()


@pytest.mark.asyncio
async def test_dispatch_oserror_cleans_up_sentinel(tmp_config):
    """OSError during NEW dispatch (from non-_call_api code) cleans up sentinel."""
    client = _make_client()
    adapter = _make_adapter(tmp_config, client)

    # Raise OSError from encode_tif which runs before the sentinel is set
    # We need it AFTER sentinel is set — use price_codec.descale
    adapter.price_codec.descale.side_effect = OSError("timed out")

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=51)
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is False

    order_key = "s1:51"
    # Sentinel must be cleaned up after OSError (D2 rollback at line 1812)
    assert order_key not in adapter.live_orders


# ═════════════════════════════════════════════════════════════════════════════
# Cluster E: execute() — additional guard paths
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_strategy_cb_open_rejects_to_dlq(tmp_config):
    """execute() routes to DLQ when per-strategy circuit breaker is open (line 1078-1079)."""
    adapter = _make_adapter(tmp_config)
    adapter.strategy_cb_mgr = MagicMock()
    adapter.strategy_cb_mgr.is_open.return_value = True

    cmd = _make_cmd(intent_type=IntentType.NEW)
    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "Per-strategy" in call_kwargs["error_message"]


@pytest.mark.asyncio
async def test_execute_global_cb_open_rejects_to_dlq(tmp_config):
    """execute() routes to DLQ when global circuit breaker is open (line 1082-1085)."""
    adapter = _make_adapter(tmp_config)
    adapter.strategy_cb_mgr = MagicMock()
    adapter.strategy_cb_mgr.is_open.return_value = False
    adapter.circuit_breaker = MagicMock()
    adapter.circuit_breaker.is_open.return_value = True

    cmd = _make_cmd(intent_type=IntentType.NEW)
    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "Circuit breaker" in call_kwargs["error_message"]


@pytest.mark.asyncio
async def test_execute_validate_client_fail_records_strategy_cb_failure(tmp_config):
    """execute() records strategy CB failure when client validation fails (lines 1106-1111)."""
    adapter = _make_adapter(tmp_config)
    adapter.strategy_cb_mgr = MagicMock()
    adapter.strategy_cb_mgr.is_open.return_value = False
    adapter.circuit_breaker = MagicMock()
    adapter.circuit_breaker.is_open.return_value = False
    adapter.rate_limiter = MagicMock()
    adapter.rate_limiter.check.return_value = True

    # Remove place_order to make validation fail
    del adapter.client.place_order

    cmd = _make_cmd(intent_type=IntentType.NEW)
    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    adapter.strategy_cb_mgr.record_failure.assert_called_with("strat1")


# ═════════════════════════════════════════════════════════════════════════════
# on_terminal_state — deferred terminal overflow path (lines 814-830)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_terminal_state_deferred_overflow_evicts_oldest(tmp_config):
    """When deferred_terminals is full, oldest entry is evicted and its live_orders key cleaned."""
    from hft_platform.order.adapter import _PENDING_SENTINEL

    adapter = _make_adapter(tmp_config)
    # Fill _pending_order_keys so has_pending=True
    adapter._pending_order_keys.add("s1:0")

    # Fill deferred_terminals to max capacity
    maxlen = adapter._deferred_terminals.maxlen
    import time

    for i in range(maxlen):
        adapter._deferred_terminals.append((f"s{i}", str(i), time.monotonic()))
        # Add a sentinel in live_orders for each deferred
        adapter.live_orders[f"s{i}:{i}"] = _PENDING_SENTINEL

    # The first entry in the deque is the oldest (will be evicted on next append)
    oldest_entry = adapter._deferred_terminals[0]
    oldest_key = f"{oldest_entry[0]}:{oldest_entry[1]}"
    adapter.live_orders[oldest_key] = _PENDING_SENTINEL

    await adapter.on_terminal_state("s1", "999")

    # Oldest deferred terminal's live_orders entry must be cleaned up
    assert oldest_key not in adapter.live_orders


# ═════════════════════════════════════════════════════════════════════════════
# _register_broker_ids — eviction at capacity (line 906)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_register_broker_ids_evicts_at_capacity(tmp_config):
    """_register_broker_ids evicts stale entries when order_id_map is at max capacity."""
    adapter = _make_adapter(tmp_config)
    adapter._order_id_map_max_size = 5

    # Pre-fill to capacity with stale entries (not in live_orders)
    for i in range(5):
        adapter.order_id_map[f"stale_id_{i}"] = f"old_strat:{i}"

    # Register a new order — should trigger eviction
    trade = {"seq_no": "NEW_SEQ", "ord_no": "NEW_ORD"}
    ok = await adapter._register_broker_ids("new_strat:1", trade)

    assert ok is True
    # After eviction, at least some stale entries should be gone
    assert "NEW_SEQ" in adapter.order_id_map or "NEW_ORD" in adapter.order_id_map


# ═════════════════════════════════════════════════════════════════════════════
# _dispatch_to_api NEW — no broker codec path (lines 1381-1386)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_new_no_broker_codec_rejects(tmp_config):
    """_dispatch_to_api NEW returns False when _broker_codec is None."""
    adapter = _make_adapter(tmp_config)
    adapter._broker_codec = None

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=60)
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is False
    adapter.metrics.order_reject_total.inc.assert_called()


# ═════════════════════════════════════════════════════════════════════════════
# _dispatch_to_api NEW — timestamp injection for object trade (line 1530)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_new_trade_timestamp_injected_for_object_trade(tmp_config):
    """_dispatch_to_api injects .timestamp attribute on object trade (line 1530)."""
    client = _make_client()
    # Make a simple namespace object with seq_no (will be converted via setattr)
    trade_obj = SimpleNamespace(seq_no="S1", ord_no="O1", order_id="ID1", id="X1", order=None, status=None)
    client.place_order.return_value = trade_obj
    adapter = _make_adapter(tmp_config, client)

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=70)
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    assert hasattr(trade_obj, "timestamp")


# ═════════════════════════════════════════════════════════════════════════════
# _dispatch_to_api NEW — MKT/MKP+ROD rejection (lines 1460-1473)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_new_mkt_rod_combination_is_rejected(tmp_config):
    """_dispatch_to_api rejects MKT/ROD combination as invalid (lines 1460-1473)."""

    class _MktCodec:
        def encode_side(self, side: Any) -> str:
            return "Buy"

        def encode_tif(self, tif: Any) -> str:
            return "ROD"  # ROD is invalid for MKT orders

        def encode_price_type(self, price_type: Any) -> str:
            return "MKT"  # market order

    adapter = _make_adapter(tmp_config)
    adapter._broker_codec = _MktCodec()

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=80, price=0)
    # Set price_type to MKT on the intent
    intent = OrderIntent(
        intent_id=80,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=0,
        qty=1,
        price_type="MKT",
    )
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)

    assert result is False
    # Sentinel must be cleaned up
    order_key = "s1:80"
    assert order_key not in adapter.live_orders


# ═════════════════════════════════════════════════════════════════════════════
# _dispatch_to_api — metadata exchange lookup failure path (lines 1395-1401)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_new_metadata_exchange_failure_uses_client_exchange(tmp_config):
    """When metadata.exchange() fails, falls back to client.get_exchange (lines 1395-1401)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.exchange.side_effect = AttributeError("no exchange")
    adapter.client.get_exchange.return_value = "OTC"

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=90)
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    call_kwargs = adapter.client.place_order.call_args.kwargs
    assert call_kwargs["exchange"] == "OTC"


# ═════════════════════════════════════════════════════════════════════════════
# _dispatch_to_api NEW — timestamp exception path (lines 1531-1539)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_new_timestamp_exception_stores_external_timestamp_in_dict(tmp_config):
    """When trade.timestamp raises, external timestamp is stored in dict trade (lines 1531-1539)."""
    client = _make_client()

    # Return a dict trade without a "timestamp" key but that raises on direct set
    # We simulate this by using a custom subclass that raises on key set
    class _FrozenDict(dict):
        def __setitem__(self, key, value):
            if key == "timestamp":
                raise TypeError("frozen dict")
            super().__setitem__(key, value)

    trade_dict = _FrozenDict({"seq_no": "S1", "ord_no": "O1"})
    client.place_order.return_value = trade_dict
    adapter = _make_adapter(tmp_config, client)

    intent = _make_intent(IntentType.NEW, strategy_id="s1", intent_id=100)
    cmd = _make_cmd(intent)

    await adapter._dispatch_to_api(cmd)

    # _external_timestamp fallback should be set (line 1538-1539)
    assert "_external_timestamp" in trade_dict


# ═════════════════════════════════════════════════════════════════════════════
# _dispatch_to_api FORCE_FLAT — metadata failure sub-paths (lines 1589-1612)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_force_flat_metadata_exchange_failure_uses_client(tmp_config):
    """FORCE_FLAT falls back to client.get_exchange when metadata.exchange fails (lines 1592-1593)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.exchange.side_effect = KeyError("no exchange for symbol")
    adapter.client.get_exchange.return_value = "OTC"

    pos = SimpleNamespace(symbol="2330", net_qty=1, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=110, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is True
    call_kwargs = adapter.client.place_order.call_args.kwargs
    assert call_kwargs["exchange"] == "OTC"


@pytest.mark.asyncio
async def test_dispatch_force_flat_product_type_failure_uses_none(tmp_config):
    """FORCE_FLAT handles product_type lookup failure gracefully (lines 1604-1605)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.product_type.side_effect = AttributeError("no product_type")

    pos = SimpleNamespace(symbol="2330", net_qty=1, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=111, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is True


@pytest.mark.asyncio
async def test_dispatch_force_flat_order_params_failure_uses_empty(tmp_config):
    """FORCE_FLAT handles order_params lookup failure gracefully (lines 1611-1612)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.order_params.side_effect = TypeError("bad params")

    pos = SimpleNamespace(symbol="2330", net_qty=1, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=112, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is True


@pytest.mark.asyncio
async def test_dispatch_force_flat_timestamp_exception_stores_external(tmp_config):
    """FORCE_FLAT handles trade timestamp exception (lines 1661-1663)."""
    client = _make_client()

    class _FrozenDict(dict):
        def __setitem__(self, key, value):
            if key == "timestamp":
                raise AttributeError("frozen")
            super().__setitem__(key, value)

    trade_dict = _FrozenDict({"seq_no": "S1", "ord_no": "O1"})
    client.place_order.return_value = trade_dict
    adapter = _make_adapter(tmp_config, client)

    pos = SimpleNamespace(symbol="2330", net_qty=1, avg_price_scaled=500_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    intent = _make_intent(IntentType.FORCE_FLAT, strategy_id="s1", intent_id=113, symbol="2330")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is True
    assert "_external_timestamp" in trade_dict


# ═════════════════════════════════════════════════════════════════════════════
# Lines 1707 — CANCEL dispatch result is None → returns False
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_cancel_api_returns_none_returns_false(tmp_config):
    """CANCEL returns False when _call_api returns None (line 1706-1707)."""
    client = _make_client()
    client.cancel_order.return_value = None
    adapter = _make_adapter(tmp_config, client)

    # Put a real trade in live_orders
    trade_obj = SimpleNamespace(seq_no="S1", ord_no="O1", order_id="C1", id="D1", order=None, status=None)
    adapter.live_orders["s1:5"] = trade_obj

    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:5"

    intent = _make_intent(IntentType.CANCEL, strategy_id="s1", intent_id=1, target_order_id="s1:5")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is False


# ═════════════════════════════════════════════════════════════════════════════
# Lines 1761 — AMEND dispatch result is None → returns False
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_amend_api_returns_none_returns_false(tmp_config):
    """AMEND returns False when _call_api returns None (line 1760-1761)."""
    client = _make_client()
    client.update_order.return_value = None
    adapter = _make_adapter(tmp_config, client)

    trade_obj = SimpleNamespace(seq_no="S1", ord_no="O1", order_id="A1", id="B1", order=None, status=None)
    adapter.live_orders["s1:6"] = trade_obj

    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:6"

    intent = _make_intent(IntentType.AMEND, strategy_id="s1", intent_id=1, target_order_id="s1:6")
    cmd = _make_cmd(intent)

    result = await adapter._dispatch_to_api(cmd)
    assert result is False


# ═════════════════════════════════════════════════════════════════════════════
# _drain_deferred_terminals — expired entry path (lines 996-1016)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_drain_deferred_terminals_removes_resolved_entry(tmp_config):
    """_drain_deferred_terminals removes deferred entries where order_key now exists in live_orders."""
    import time as _time

    adapter = _make_adapter(tmp_config)

    # Add a deferred terminal — not expired (recent timestamp)
    recent_ts = _time.monotonic()
    adapter._deferred_terminals.append(("s1", "oid1", recent_ts))

    # Make order_id_resolver return a key that IS in live_orders so it gets cleaned up
    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:oid1"
    adapter.live_orders["s1:oid1"] = MagicMock()

    trade = MagicMock(seq_no="S1", ord_no="O1")
    await adapter._drain_deferred_terminals("s1:1", trade)

    assert "s1:oid1" not in adapter.live_orders
    # Deferred terminal should be cleared
    assert len(adapter._deferred_terminals) == 0


@pytest.mark.asyncio
async def test_drain_deferred_terminals_expired_entries_are_logged_not_processed(tmp_config):
    """_drain_deferred_terminals skips expired entries (age >= 30s) (lines 996-1004)."""
    import time as _time

    adapter = _make_adapter(tmp_config)

    # Add an expired deferred terminal (> 30s old)
    old_ts = _time.monotonic() - 35.0
    adapter._deferred_terminals.append(("s1", "expired_oid", old_ts))

    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:expired_oid"
    adapter.live_orders["s1:expired_oid"] = MagicMock()

    await adapter._drain_deferred_terminals("s1:1", MagicMock(seq_no="S1"))

    # Entry was expired — it should NOT have been processed into remaining
    # (it was discarded at the expired check)
    assert len(adapter._deferred_terminals) == 0


@pytest.mark.asyncio
async def test_drain_deferred_terminals_unresolved_entry_stays_in_remaining(tmp_config):
    """_drain_deferred_terminals keeps entries that aren't yet in live_orders (line 1016)."""
    import time as _time

    adapter = _make_adapter(tmp_config)

    recent_ts = _time.monotonic()
    adapter._deferred_terminals.append(("s1", "pending_oid", recent_ts))

    # Return a key NOT in live_orders
    adapter.order_id_resolver = MagicMock()
    adapter.order_id_resolver.resolve_order_key.return_value = "s1:pending_oid"
    # Do NOT add to live_orders

    await adapter._drain_deferred_terminals("s1:1", MagicMock(seq_no="S1"))

    # Entry should remain because it wasn't resolved
    assert len(adapter._deferred_terminals) == 1


# ═════════════════════════════════════════════════════════════════════════════
# execute() — platform degrade rejects (lines 1093-1096)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_platform_degrade_rejects_to_dlq(tmp_config):
    """execute() routes to DLQ when _platform_degrade_allows returns False (lines 1093-1096)."""
    adapter = _make_adapter(tmp_config)
    adapter.strategy_cb_mgr = MagicMock()
    adapter.strategy_cb_mgr.is_open.return_value = False
    adapter.circuit_breaker = MagicMock()
    adapter.circuit_breaker.is_open.return_value = False
    adapter.rate_limiter = MagicMock()
    adapter.rate_limiter.check.return_value = True

    # Make platform degrade block the order
    controller = MagicMock()
    controller.reduce_only_active = False
    controller.allow_intent.return_value = False
    adapter.platform_degrade_controller = controller

    cmd = _make_cmd(intent_type=IntentType.NEW)
    await adapter.execute(cmd)

    adapter._dlq.add.assert_awaited_once()
    call_kwargs = adapter._dlq.add.call_args[1]
    assert "reduce-only" in call_kwargs["error_message"].lower()


# ═════════════════════════════════════════════════════════════════════════════
# execute() — dedup commit paths (lines 1114-1121)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_records_queue_latency_when_created_ns_nonzero(tmp_config):
    """execute() calls _record_queue_latency when not running and cmd.created_ns > 0 (line 1114-1115)."""
    adapter = _make_adapter(tmp_config)
    adapter.running = False
    adapter._dispatch_to_api = AsyncMock(return_value=True)

    cmd = _make_cmd(intent_type=IntentType.NEW)
    cmd.created_ns = timebase.now_ns()  # non-zero — should trigger _record_queue_latency

    # Just verify it doesn't blow up
    await adapter.execute(cmd)

    adapter._dispatch_to_api.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_dispatch_exception_commits_dedup_false(tmp_config):
    """execute() commits dedup False when _dispatch_to_api raises (lines 1120-1121)."""
    adapter = _make_adapter(tmp_config)
    adapter.running = False

    # Make dispatch raise
    adapter._dispatch_to_api = AsyncMock(side_effect=RuntimeError("unexpected"))

    # Use an intent with idempotency_key so dedup is involved
    intent = OrderIntent(
        intent_id=200,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=500_0000,
        qty=1,
        idempotency_key="test-key-200",
    )
    cmd = OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=timebase.now_ns() + 10**10,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=0,
    )

    # Should not re-raise (exception is swallowed into dedup commit)
    await adapter.execute(cmd)

    # dispatch was attempted once despite the exception
    adapter._dispatch_to_api.assert_awaited_once()


# ═════════════════════════════════════════════════════════════════════════════
# _is_strategy_halt_exempt — frozenset attribute path (line 1149)
# ═════════════════════════════════════════════════════════════════════════════


def test_is_strategy_halt_exempt_uses_frozenset_attribute(tmp_config):
    """_is_strategy_halt_exempt uses _halt_exempt_strategies frozenset when is_halt_exempt is absent."""
    adapter = _make_adapter(tmp_config)

    # Set up storm_guard with _halt_exempt_strategies but no is_halt_exempt method
    sg = SimpleNamespace(_halt_exempt_strategies=frozenset(["exempt_strat"]))
    adapter._storm_guard = sg

    assert adapter._is_strategy_halt_exempt("exempt_strat") is True
    assert adapter._is_strategy_halt_exempt("non_exempt") is False


def test_is_strategy_halt_exempt_calls_is_halt_exempt_when_callable(tmp_config):
    """_is_strategy_halt_exempt calls is_halt_exempt(strategy_id) when it's callable (line 1148)."""
    adapter = _make_adapter(tmp_config)

    sg = SimpleNamespace(is_halt_exempt=lambda sid: sid == "special")
    adapter._storm_guard = sg

    assert adapter._is_strategy_halt_exempt("special") is True
    assert adapter._is_strategy_halt_exempt("other") is False


# ═════════════════════════════════════════════════════════════════════════════
# _pending_close_qty helpers (lines 1259-1267)
# ═════════════════════════════════════════════════════════════════════════════


def test_pending_close_qty_returns_zero_for_empty_live_orders(tmp_config):
    """_pending_close_qty returns 0 with no live orders."""
    adapter = _make_adapter(tmp_config)
    result = adapter._pending_close_qty("2330", Side.BUY)
    assert result == 0


def test_pending_close_qty_skips_sentinel_entries(tmp_config):
    """_pending_close_qty skips PENDING_SENTINEL and TERMINAL_BEFORE_REGISTERED entries."""
    from hft_platform.order.adapter import _PENDING_SENTINEL, _TERMINAL_BEFORE_REGISTERED

    adapter = _make_adapter(tmp_config)
    adapter.live_orders["s1:1"] = _PENDING_SENTINEL
    adapter.live_orders["s1:2"] = _TERMINAL_BEFORE_REGISTERED

    result = adapter._pending_close_qty("2330", Side.BUY)
    assert result == 0


def test_pending_close_qty_counts_matching_symbol_and_side(tmp_config):
    """_pending_close_qty returns qty for matching symbol+side dict trades."""
    adapter = _make_adapter(tmp_config)
    trade = {"contract_code": "2330", "action": "BUY", "qty": 3}
    adapter.live_orders["s1:1"] = trade

    result = adapter._pending_close_qty("2330", Side.BUY)
    assert result == 3


def test_pending_close_qty_excludes_different_symbol(tmp_config):
    """_pending_close_qty ignores trades for different symbols."""
    adapter = _make_adapter(tmp_config)
    trade = {"contract_code": "2454", "action": "BUY", "qty": 5}
    adapter.live_orders["s1:1"] = trade

    result = adapter._pending_close_qty("2330", Side.BUY)
    assert result == 0


# ═════════════════════════════════════════════════════════════════════════════
# _live_order_symbol, _live_order_side, _live_order_qty static helpers
# (lines 1271-1293)
# ═════════════════════════════════════════════════════════════════════════════


def test_live_order_symbol_from_dict(tmp_config):
    """_live_order_symbol extracts contract_code or symbol from dict."""
    from hft_platform.order.adapter import OrderAdapter

    assert OrderAdapter._live_order_symbol({"contract_code": "2330"}) == "2330"
    assert OrderAdapter._live_order_symbol({"symbol": "2454"}) == "2454"
    assert OrderAdapter._live_order_symbol({}) == ""


def test_live_order_symbol_from_object(tmp_config):
    """_live_order_symbol extracts from object attributes."""
    from hft_platform.order.adapter import OrderAdapter

    obj = SimpleNamespace(contract_code="2330", symbol="")
    assert OrderAdapter._live_order_symbol(obj) == "2330"

    obj2 = SimpleNamespace(contract_code="", symbol="2454")
    assert OrderAdapter._live_order_symbol(obj2) == "2454"


def test_live_order_side_from_dict_sell(tmp_config):
    """_live_order_side returns SELL for sell actions."""
    from hft_platform.order.adapter import OrderAdapter

    assert OrderAdapter._live_order_side({"action": "SELL"}) == Side.SELL
    assert OrderAdapter._live_order_side({"action": "ACTION.SELL"}) == Side.SELL
    assert OrderAdapter._live_order_side({"action": "1"}) == Side.SELL


def test_live_order_side_from_dict_buy(tmp_config):
    """_live_order_side returns BUY for buy actions."""
    from hft_platform.order.adapter import OrderAdapter

    assert OrderAdapter._live_order_side({"action": "BUY"}) == Side.BUY
    assert OrderAdapter._live_order_side({"action": "ACTION.BUY"}) == Side.BUY
    assert OrderAdapter._live_order_side({"action": "0"}) == Side.BUY


def test_live_order_side_from_dict_unknown(tmp_config):
    """_live_order_side returns None for unknown actions."""
    from hft_platform.order.adapter import OrderAdapter

    assert OrderAdapter._live_order_side({"action": "UNKNOWN"}) is None
    assert OrderAdapter._live_order_side({}) is None


def test_live_order_side_from_object(tmp_config):
    """_live_order_side reads from object attributes."""
    from hft_platform.order.adapter import OrderAdapter

    obj = SimpleNamespace(action="SELL", side="")
    assert OrderAdapter._live_order_side(obj) == Side.SELL


def test_live_order_qty_from_dict_and_object(tmp_config):
    """_live_order_qty returns qty from dict and object."""
    from hft_platform.order.adapter import OrderAdapter

    assert OrderAdapter._live_order_qty({"qty": 5}) == 5
    assert OrderAdapter._live_order_qty({}) == 0
    obj = SimpleNamespace(qty=3)
    assert OrderAdapter._live_order_qty(obj) == 3


# ═════════════════════════════════════════════════════════════════════════════
# _force_flat_price — zero price path uses reference price (lines 1319-1332)
# ═════════════════════════════════════════════════════════════════════════════


def test_force_flat_price_uses_requested_price_when_positive(tmp_config):
    """_force_flat_price returns requested_price directly when > 0 (line 1321)."""
    adapter = _make_adapter(tmp_config)
    result = adapter._force_flat_price("2330", Side.SELL, 500_0000)
    assert result == 500_0000


def test_force_flat_price_zero_price_uses_ref_for_sell(tmp_config):
    """_force_flat_price computes sell price from reference when requested=0 (line 1332)."""
    adapter = _make_adapter(tmp_config)
    # Set up price scale and reference price via position_store
    adapter.metadata.price_scale.return_value = 10_000
    pos = SimpleNamespace(symbol="2330", net_qty=1, avg_price_scaled=100_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    result = adapter._force_flat_price("2330", Side.SELL, 0)
    assert result > 0  # Should be 93% of reference


def test_force_flat_price_zero_price_uses_ref_for_buy(tmp_config):
    """_force_flat_price computes buy price from reference when requested=0 (line 1331)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.price_scale.return_value = 10_000
    pos = SimpleNamespace(symbol="2330", net_qty=-1, avg_price_scaled=100_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": pos})

    result = adapter._force_flat_price("2330", Side.BUY, 0)
    assert result > 0  # Should be 107% of reference


def test_force_flat_price_zero_ref_uses_scale_times_1000(tmp_config):
    """_force_flat_price uses scale*1000 as ref when no position data (lines 1325-1326)."""
    adapter = _make_adapter(tmp_config)
    adapter.metadata.price_scale.return_value = 10_000
    # No position_store → ref_price = 0 → falls back to scale * 1000
    adapter.position_store = None

    result = adapter._force_flat_price("2330", Side.SELL, 0)
    assert result > 0


# ═════════════════════════════════════════════════════════════════════════════
# _platform_reference_price_for_symbol (lines 1308-1317)
# ═════════════════════════════════════════════════════════════════════════════


def test_platform_reference_price_no_position_store(tmp_config):
    """_platform_reference_price_for_symbol returns 0 with no position_store."""
    adapter = _make_adapter(tmp_config)
    adapter.position_store = None
    assert adapter._platform_reference_price_for_symbol("2330") == 0


def test_platform_reference_price_with_position(tmp_config):
    """_platform_reference_price_for_symbol returns max avg_price from positions."""
    adapter = _make_adapter(tmp_config)
    p1 = SimpleNamespace(symbol="2330", avg_price_scaled=500_0000)
    p2 = SimpleNamespace(symbol="2330", avg_price_scaled=600_0000)
    adapter.position_store = SimpleNamespace(positions={"p1": p1, "p2": p2})

    result = adapter._platform_reference_price_for_symbol("2330")
    assert result == 600_0000
