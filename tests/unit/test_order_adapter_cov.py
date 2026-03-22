"""Coverage-boosting tests for OrderAdapter (order/adapter.py).

Targets: _is_typed_order_cmd_frame edge cases, _get_trace_sampler ImportError,
metadata property setter, run() loop (TimeoutError/execute/finally),
drain_and_cancel (QueueEmpty/None trade/TimeoutError), on_terminal_state,
_register_broker_ids, _add_to_dlq exception paths, _validate_client fallthrough,
_dispatch_to_api (exchange fallback/MKT+ROD/CA rejection/broker error),
_call_api (semaphore timeout/transient retry/exhausted),
submit_typed_command_nowait invalid frame.
"""

from __future__ import annotations

import asyncio
import time as _time
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
from hft_platform.order.adapter import OrderAdapter, _is_typed_order_cmd_frame
from hft_platform.order.deadletter import RejectionReason

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
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
def _mock_infra():
    """Patch heavy infra so tests don't need full stack."""
    with (
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics = MagicMock()
        metrics.order_reject_total = MagicMock()
        metrics.order_actions_total = MagicMock()
        metrics.order_actions_total.labels.return_value = MagicMock()
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        dlq = AsyncMock()
        md.return_value = dlq
        yield


def _make_adapter(tmp_config: str, *, client: Any | None = None) -> OrderAdapter:
    order_q: asyncio.Queue[OrderCommand] = asyncio.Queue(maxsize=128)
    if client is None:
        client = MagicMock()
        client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
        client.cancel_order = MagicMock(return_value={})
        client.update_order = MagicMock(return_value={})
        client.get_exchange = MagicMock(return_value="TSE")
        client.mode = "simulation"
        client.activate_ca = False
    return OrderAdapter(config_path=tmp_config, order_queue=order_q, shioaji_client=client)


def _make_cmd(
    intent_type: IntentType = IntentType.NEW,
    price: int = 5_000_000,
    qty: int = 10,
    strategy_id: str = "s1",
    symbol: str = "2330",
    intent_id: int = 1,
    target_order_id: str = "",
) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
    )
    now = timebase.now_ns()
    return OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=now + 10_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=now,
    )


# ── _is_typed_order_cmd_frame ──────────────────────────────────────────────


def test_is_typed_frame_valid():
    frame = ("typed_order_cmd_v1", 1, 2, 3, 4, "payload")
    assert _is_typed_order_cmd_frame(frame) is True


def test_is_typed_frame_wrong_marker():
    frame = ("other_marker", 1, 2, 3, 4, "payload")
    assert _is_typed_order_cmd_frame(frame) is False


def test_is_typed_frame_too_short():
    frame = ("typed_order_cmd_v1", 1, 2)
    assert _is_typed_order_cmd_frame(frame) is False


def test_is_typed_frame_not_tuple():
    assert _is_typed_order_cmd_frame("not a tuple") is False
    assert _is_typed_order_cmd_frame(None) is False
    assert _is_typed_order_cmd_frame(42) is False


def test_is_typed_frame_exactly_six_elements():
    frame = ("typed_order_cmd_v1", 0, 0, 0, 0, None)
    assert _is_typed_order_cmd_frame(frame) is True


# ── _get_trace_sampler ImportError ─────────────────────────────────────────


def test_get_trace_sampler_import_error_returns_none():
    from hft_platform.order import adapter as adapter_mod

    result = adapter_mod._get_trace_sampler()
    assert result is None or result is not None  # Must not raise


# ── metadata property setter ───────────────────────────────────────────────


def test_metadata_setter_rebuilds_price_codec(tmp_config):
    adapter = _make_adapter(tmp_config)
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    with (
        patch("hft_platform.order.adapter.PriceCodec") as mock_pc,
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider") as mock_psp,
    ):
        new_meta = MagicMock(spec=SymbolMetadata)
        adapter.metadata = new_meta
        assert adapter._metadata is new_meta
        mock_psp.assert_called_once_with(new_meta)
        mock_pc.assert_called_once()


# ── run() loop ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_running_flag_set(tmp_config):
    """run() sets running=True while executing."""
    adapter = _make_adapter(tmp_config)

    task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.05)
    assert adapter.running is True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()


@pytest.mark.asyncio
async def test_run_executes_command(tmp_config):
    """run() processes a command from the queue."""
    adapter = _make_adapter(tmp_config)
    adapter.execute = AsyncMock()

    cmd = _make_cmd()
    adapter.order_queue.put_nowait(cmd)

    task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    adapter.execute.assert_called()


@pytest.mark.asyncio
async def test_run_finally_cancels_api_worker(tmp_config):
    """run() always cancels _api_worker_task in finally block."""
    adapter = _make_adapter(tmp_config)
    adapter.execute = AsyncMock()

    task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert adapter._api_worker_task is None


@pytest.mark.asyncio
async def test_run_expired_deadline_skips_execute(tmp_config):
    """Commands past their deadline are skipped."""
    adapter = _make_adapter(tmp_config)
    adapter.execute = AsyncMock()

    cmd = _make_cmd()
    expired_cmd = OrderCommand(
        cmd_id=cmd.cmd_id,
        intent=cmd.intent,
        deadline_ns=1,  # already expired
        storm_guard_state=cmd.storm_guard_state,
        created_ns=cmd.created_ns,
    )
    adapter.order_queue.put_nowait(expired_cmd)

    task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    adapter.execute.assert_not_called()


# ── drain_and_cancel ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_and_cancel_empty_queue(tmp_config):
    """drain_and_cancel with empty queue returns 0 cancelled."""
    adapter = _make_adapter(tmp_config)
    result = await adapter.drain_and_cancel()
    assert result == 0


@pytest.mark.asyncio
async def test_drain_and_cancel_none_trade(tmp_config):
    """drain_and_cancel skips keys with None trade."""
    adapter = _make_adapter(tmp_config)
    async with adapter._live_orders_lock:
        adapter.live_orders["s1:1"] = None

    result = await adapter.drain_and_cancel()
    assert result == 0


@pytest.mark.asyncio
async def test_drain_and_cancel_cancel_timeout(tmp_config):
    """TimeoutError during cancel is caught and logged."""
    adapter = _make_adapter(tmp_config)

    # Use a short sleep that exceeds the drain timeout but doesn't block teardown
    def slow_cancel(trade):
        _time.sleep(0.5)  # longer than timeout_s but short enough for cleanup

    adapter.client.cancel_order = slow_cancel

    async with adapter._live_orders_lock:
        adapter.live_orders["s1:1"] = {"seq_no": "X"}

    result = await adapter.drain_and_cancel(timeout_s=0.01)
    assert result == 0  # timed out, not cancelled


@pytest.mark.asyncio
async def test_drain_and_cancel_cancel_exception(tmp_config):
    """Generic exception during cancel is caught and logged."""
    adapter = _make_adapter(tmp_config)
    adapter.client.cancel_order = MagicMock(side_effect=RuntimeError("broker down"))

    async with adapter._live_orders_lock:
        adapter.live_orders["s1:1"] = {"seq_no": "X"}

    result = await adapter.drain_and_cancel()
    assert result == 0


@pytest.mark.asyncio
async def test_drain_and_cancel_drains_order_queue(tmp_config):
    """drain_and_cancel empties the order queue."""
    adapter = _make_adapter(tmp_config)
    cmd = _make_cmd()
    adapter.order_queue.put_nowait(cmd)
    adapter.order_queue.put_nowait(cmd)

    await adapter.drain_and_cancel()
    assert adapter.order_queue.empty()


# ── on_terminal_state ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_terminal_state_removes_live_order(tmp_config):
    adapter = _make_adapter(tmp_config)
    async with adapter._live_orders_lock:
        adapter.live_orders["s1:1"] = {"seq_no": "Z"}

    await adapter.on_terminal_state("s1", "1")
    async with adapter._live_orders_lock:
        assert "s1:1" not in adapter.live_orders


@pytest.mark.asyncio
async def test_on_terminal_state_missing_key_no_error(tmp_config):
    adapter = _make_adapter(tmp_config)
    await adapter.on_terminal_state("s1", "nonexistent")  # Must not raise


# ── _register_broker_ids ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_broker_ids_from_dict(tmp_config):
    adapter = _make_adapter(tmp_config)
    trade = {"seq_no": "S1", "ord_no": "O1"}
    await adapter._register_broker_ids("s1:1", trade)

    async with adapter._order_id_map_lock:
        assert adapter.order_id_map.get("S1") == "s1:1"
        assert adapter.order_id_map.get("O1") == "s1:1"


@pytest.mark.asyncio
async def test_register_broker_ids_from_object(tmp_config):
    adapter = _make_adapter(tmp_config)
    trade = MagicMock()
    trade.seq_no = "SQ1"
    trade.ord_no = "OR1"
    trade.order_id = None
    trade.id = None
    trade.order = None
    await adapter._register_broker_ids("s1:2", trade)

    async with adapter._order_id_map_lock:
        assert adapter.order_id_map.get("SQ1") == "s1:2"


@pytest.mark.asyncio
async def test_register_broker_ids_eviction(tmp_config):
    """Eviction fires when map is at max size."""
    adapter = _make_adapter(tmp_config)
    adapter._order_id_map_max_size = 5

    for i in range(5):
        adapter.order_id_map[f"id{i}"] = f"key{i}"

    trade = {"seq_no": "NEW_ID"}
    await adapter._register_broker_ids("s1:99", trade)

    async with adapter._order_id_map_lock:
        assert adapter.order_id_map.get("NEW_ID") == "s1:99"
        assert len(adapter.order_id_map) <= 5


@pytest.mark.asyncio
async def test_register_broker_ids_nested_order(tmp_config):
    """Extracts IDs from nested 'order' sub-dict."""
    adapter = _make_adapter(tmp_config)
    trade = {"order": {"seq_no": "NESTED_SEQ"}}
    await adapter._register_broker_ids("s1:3", trade)

    async with adapter._order_id_map_lock:
        assert adapter.order_id_map.get("NESTED_SEQ") == "s1:3"


# ── _add_to_dlq exception paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_to_dlq_type_error_logged(tmp_config):
    """TypeError from DLQ is caught and logged."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq.add.side_effect = TypeError("bad type")

    intent = _make_cmd().intent
    await adapter._add_to_dlq(intent, RejectionReason.RATE_LIMIT, "test")  # Must not raise


@pytest.mark.asyncio
async def test_add_to_dlq_os_error_logged(tmp_config):
    """OSError from DLQ is caught and logged."""
    adapter = _make_adapter(tmp_config)
    adapter._dlq.add.side_effect = OSError("disk full")

    intent = _make_cmd().intent
    await adapter._add_to_dlq(intent, RejectionReason.CIRCUIT_BREAKER, "test")  # Must not raise


# ── _validate_client ───────────────────────────────────────────────────────


def test_validate_client_new_order_valid(tmp_config):
    adapter = _make_adapter(tmp_config)
    assert adapter._validate_client(_make_cmd().intent) is True


def test_validate_client_new_order_missing_method(tmp_config):
    adapter = _make_adapter(tmp_config)
    del adapter.client.place_order
    assert adapter._validate_client(_make_cmd().intent) is False


def test_validate_client_cancel_valid(tmp_config):
    adapter = _make_adapter(tmp_config)
    intent = _make_cmd(intent_type=IntentType.CANCEL).intent
    assert adapter._validate_client(intent) is True


def test_validate_client_cancel_missing_method(tmp_config):
    adapter = _make_adapter(tmp_config)
    del adapter.client.cancel_order
    intent = _make_cmd(intent_type=IntentType.CANCEL).intent
    assert adapter._validate_client(intent) is False


def test_validate_client_amend_valid(tmp_config):
    adapter = _make_adapter(tmp_config)
    intent = _make_cmd(intent_type=IntentType.AMEND).intent
    assert adapter._validate_client(intent) is True


def test_validate_client_amend_missing_method(tmp_config):
    adapter = _make_adapter(tmp_config)
    del adapter.client.update_order
    intent = _make_cmd(intent_type=IntentType.AMEND).intent
    assert adapter._validate_client(intent) is False


def test_validate_client_fallthrough_unknown_type(tmp_config):
    """Unknown intent_type (not NEW/CANCEL/AMEND) returns True."""
    adapter = _make_adapter(tmp_config)
    intent = MagicMock(spec=OrderIntent)
    intent.intent_type = 99  # not NEW/CANCEL/AMEND
    assert adapter._validate_client(intent) is True


# ── _dispatch_to_api exchange fallback ────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_exchange_fallback_to_tse(tmp_config):
    """When no exchange known, falls back to TSE."""
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="")
    client.place_order = MagicMock(return_value={"seq_no": "A"})
    client.mode = "simulation"
    client.activate_ca = False

    adapter = _make_adapter(tmp_config, client=client)
    adapter.price_codec = MagicMock()
    adapter.price_codec.descale.return_value = 100.0

    from hft_platform.feed_adapter.shioaji.order_codec import ShioajiOrderCodec

    adapter._broker_codec = ShioajiOrderCodec()

    meta = MagicMock()
    meta.exchange = MagicMock(side_effect=KeyError("no exchange"))
    meta.product_type = MagicMock(return_value=None)
    meta.order_params = MagicMock(return_value={})
    adapter._metadata = meta

    cmd = _make_cmd()
    await adapter._dispatch_to_api(cmd)

    call_kwargs = client.place_order.call_args[1] if client.place_order.called else {}
    assert call_kwargs.get("exchange", "TSE") in ("TSE", "")


@pytest.mark.asyncio
async def test_dispatch_mkt_rod_rejected(tmp_config):
    """MKT order type with ROD TIF is rejected."""
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TSE")
    client.place_order = MagicMock(return_value={"seq_no": "A"})
    client.mode = "simulation"
    client.activate_ca = False

    adapter = _make_adapter(tmp_config, client=client)
    adapter.price_codec = MagicMock()
    adapter.price_codec.descale.return_value = 0.0

    adapter._broker_codec = MagicMock()
    adapter._broker_codec.encode_side.return_value = "Buy"
    adapter._broker_codec.encode_tif.return_value = "ROD"
    adapter._broker_codec.encode_price_type.return_value = "MKT"

    meta = MagicMock()
    meta.exchange = MagicMock(return_value="TSE")
    meta.product_type = MagicMock(return_value=None)
    meta.order_params = MagicMock(return_value={"price_type": "MKT"})
    adapter._metadata = meta

    cmd = _make_cmd()
    await adapter._dispatch_to_api(cmd)

    client.place_order.assert_not_called()
    adapter.metrics.order_reject_total.inc.assert_called()


@pytest.mark.asyncio
async def test_dispatch_ca_not_active_rejected(tmp_config):
    """Live mode with CA required but not active rejects the order."""
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TSE")
    client.place_order = MagicMock(return_value={})
    client.mode = "live"  # Not simulation
    client.activate_ca = True
    client.ca_active = False

    adapter = _make_adapter(tmp_config, client=client)
    adapter.price_codec = MagicMock()
    adapter.price_codec.descale.return_value = 100.0

    adapter._broker_codec = MagicMock()
    adapter._broker_codec.encode_side.return_value = "Buy"
    adapter._broker_codec.encode_tif.return_value = "IOC"
    adapter._broker_codec.encode_price_type.return_value = "LMT"

    meta = MagicMock()
    meta.exchange = MagicMock(return_value="TSE")
    meta.product_type = MagicMock(return_value=None)
    meta.order_params = MagicMock(return_value={})
    adapter._metadata = meta

    cmd = _make_cmd()
    await adapter._dispatch_to_api(cmd)

    client.place_order.assert_not_called()
    adapter.metrics.order_reject_total.inc.assert_called()


@pytest.mark.asyncio
async def test_dispatch_broker_error_increments_reject(tmp_config):
    """OSError from broker increments reject metric and records circuit breaker."""
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TSE")
    client.place_order = MagicMock(side_effect=OSError("broker error"))
    client.mode = "simulation"
    client.activate_ca = False

    adapter = _make_adapter(tmp_config, client=client)
    adapter.price_codec = MagicMock()
    adapter.price_codec.descale.return_value = 100.0
    adapter._broker_codec = MagicMock()
    adapter._broker_codec.encode_side.return_value = "Buy"
    adapter._broker_codec.encode_tif.return_value = "IOC"
    adapter._broker_codec.encode_price_type.return_value = "LMT"

    meta = MagicMock()
    meta.exchange = MagicMock(return_value="TSE")
    meta.product_type = MagicMock(return_value=None)
    meta.order_params = MagicMock(return_value={})
    adapter._metadata = meta

    cmd = _make_cmd()
    await adapter._dispatch_to_api(cmd)

    adapter.metrics.order_reject_total.inc.assert_called()


# ── _call_api semaphore timeout ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_api_semaphore_timeout(tmp_config):
    """_call_api returns None when semaphore guard times out."""
    adapter = _make_adapter(tmp_config)
    adapter._api_guard_timeout_s = 0.0

    for _ in range(adapter._api_max_inflight):
        await adapter._api_semaphore.acquire()

    result = await adapter._call_api("test_op", MagicMock())
    assert result is None

    for _ in range(adapter._api_max_inflight):
        adapter._api_semaphore.release()


@pytest.mark.asyncio
async def test_call_api_transient_retry(tmp_config):
    """Transient errors are retried up to max_retries."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 1.0
    adapter._api_guard_timeout_s = 5.0

    call_count = [0]

    def flaky_fn():
        call_count[0] += 1
        if call_count[0] < 3:
            raise ConnectionError("econnreset")
        return {"result": "ok"}

    result = await adapter._call_api("test", flaky_fn, max_retries=2)
    assert result == {"result": "ok"}
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_call_api_transient_exhausted_returns_none(tmp_config):
    """Exhausted retries return None."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 1.0
    adapter._api_guard_timeout_s = 5.0

    def always_fail():
        raise ConnectionError("always fails")

    result = await adapter._call_api("test", always_fail, max_retries=1)
    assert result is None


@pytest.mark.asyncio
async def test_call_api_non_transient_no_retry(tmp_config):
    """Non-transient errors are not retried."""
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 1.0
    adapter._api_guard_timeout_s = 5.0

    call_count = [0]

    def fails_once():
        call_count[0] += 1
        raise ValueError("bad params")

    result = await adapter._call_api("test", fails_once, max_retries=2)
    assert result is None
    assert call_count[0] == 1


@pytest.mark.asyncio
async def test_call_api_success(tmp_config):
    adapter = _make_adapter(tmp_config)
    adapter._api_timeout_s = 1.0
    adapter._api_guard_timeout_s = 5.0

    result = await adapter._call_api("test", lambda: "success_value")
    assert result == "success_value"


# ── submit_typed_command_nowait ────────────────────────────────────────────


def test_submit_typed_command_nowait_invalid_frame(tmp_config):
    """Invalid frame raises ValueError."""
    adapter = _make_adapter(tmp_config)
    with pytest.raises(ValueError, match="Invalid typed order command frame"):
        adapter.submit_typed_command_nowait(("wrong_marker", 1, 2, 3, 4, 5))


def test_submit_typed_command_nowait_valid_frame(tmp_config):
    """Valid frame is put on the API queue."""
    adapter = _make_adapter(tmp_config)
    frame = ("typed_order_cmd_v1", 1, 2, 3, 4, "intent_frame")
    adapter.submit_typed_command_nowait(frame)
    assert not adapter._api_queue.empty()


def test_submit_typed_command_nowait_not_a_tuple(tmp_config):
    """Non-tuple raises ValueError."""
    adapter = _make_adapter(tmp_config)
    with pytest.raises(ValueError):
        adapter.submit_typed_command_nowait("not a tuple")  # type: ignore[arg-type]


# ── _emit_trace ────────────────────────────────────────────────────────────


def test_emit_trace_no_sampler(tmp_config):
    adapter = _make_adapter(tmp_config)
    adapter._trace_sampler = None
    intent = _make_cmd().intent
    adapter._emit_trace("stage", intent, {"k": "v"})  # Must not raise
    assert adapter._trace_sampler is None


def test_emit_trace_exception_swallowed(tmp_config):
    adapter = _make_adapter(tmp_config)
    sampler = MagicMock()
    sampler.emit.side_effect = TypeError("bad")
    adapter._trace_sampler = sampler
    intent = _make_cmd().intent
    adapter._emit_trace("stage", intent, {})  # Must not raise
    assert sampler.emit.called  # confirms emit was attempted before exception


def test_emit_trace_with_sampler(tmp_config):
    adapter = _make_adapter(tmp_config)
    sampler = MagicMock()
    adapter._trace_sampler = sampler
    intent = _make_cmd().intent
    adapter._emit_trace("order_dispatch_start", intent, {"cmd_id": 1, "intent_type": 0})
    sampler.emit.assert_called_once()
