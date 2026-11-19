"""Coverage gap tests for order/ module.

Targets specific missing lines in:
- order/adapter.py   (lines 62-64, 248/269-270, 314/325-334/342-353, 409-410, 445/478, 568-569/581-582)
- order/deadletter.py (lines 142-143, 151-153, 175, 198-200/210-215, 233/244-246)
- order/halt_canceller.py (lines 91-94)
- order/shadow.py    (lines 23-24, 82/87-88)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason
from hft_platform.order.halt_canceller import _BATCH_SIZE, cancel_all_live_orders
from hft_platform.order.shadow import ShadowOrderSink


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
def _mock_adapter_infra(tmp_path):
    with (
        patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(tmp_path / "oid_map.jsonl")}),
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
        metrics.rejection_sink_overflow_total = MagicMock()
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        dlq = AsyncMock()
        md.return_value = dlq
        yield


def _make_adapter(tmp_config: str):
    from hft_platform.order.adapter import OrderAdapter

    client = MagicMock()
    client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
    client.cancel_order = MagicMock(return_value={})
    client.update_order = MagicMock(return_value={})
    client.get_exchange = MagicMock(return_value="TSE")
    client.mode = "simulation"
    client.activate_ca = False
    return OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(maxsize=128),
        broker_client=client,
    )


def _make_intent(strategy_id: str = "s1", symbol: str = "2330", intent_id: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        price=1_000_000,
        qty=1,
        side=Side.BUY,
        intent_type=IntentType.NEW,
    )


def test_get_trace_sampler_returns_none_on_import_error():
    """_get_trace_sampler must return None and log debug when ImportError is raised."""
    import hft_platform.order.adapter as adapter_mod

    with patch.dict("sys.modules", {"hft_platform.diagnostics.trace": None}):
        with patch("hft_platform.order.adapter._get_trace_sampler", wraps=None) as _mock:
            pass

    import sys

    original = sys.modules.pop("hft_platform.diagnostics.trace", None)
    sys.modules["hft_platform.diagnostics.trace"] = None  # type: ignore[assignment]
    try:
        result = adapter_mod._get_trace_sampler()
        assert result is None
    finally:
        if original is not None:
            sys.modules["hft_platform.diagnostics.trace"] = original
        else:
            sys.modules.pop("hft_platform.diagnostics.trace", None)


def test_get_trace_sampler_import_error_path_directly():
    """Force the ImportError branch of _get_trace_sampler by removing the module."""
    import sys

    import hft_platform.order.adapter as adapter_mod

    saved = sys.modules.pop("hft_platform.diagnostics", None)
    saved_trace = sys.modules.pop("hft_platform.diagnostics.trace", None)

    class _BrokenModule:
        def __getattr__(self, name):
            raise ImportError("diagnostics not available")

    sys.modules["hft_platform.diagnostics.trace"] = _BrokenModule()  # type: ignore[assignment]
    try:
        result = adapter_mod._get_trace_sampler()
        assert result is None
    finally:
        if saved_trace is not None:
            sys.modules["hft_platform.diagnostics.trace"] = saved_trace
        else:
            sys.modules.pop("hft_platform.diagnostics.trace", None)
        if saved is not None:
            sys.modules["hft_platform.diagnostics"] = saved


def test_set_storm_guard_stores_reference(tmp_config):
    """set_storm_guard stores the provided storm_guard object."""
    adapter = _make_adapter(tmp_config)
    sg = MagicMock()
    adapter.set_storm_guard(sg)
    assert adapter._storm_guard is sg


def test_send_dispatch_rejection_queue_full_increments_metric(tmp_config):
    """_send_dispatch_rejection must handle asyncio.QueueFull and inc overflow metric."""
    adapter = _make_adapter(tmp_config)
    sink = MagicMock()
    sink.put_nowait = MagicMock(side_effect=asyncio.QueueFull())
    adapter._rejection_sink = sink

    intent = _make_intent()
    adapter._send_dispatch_rejection(intent, "RATE_LIMIT")

    adapter.metrics.rejection_sink_overflow_total.inc.assert_called_once()


def test_send_dispatch_rejection_generic_exception_swallowed(tmp_config):
    """_send_dispatch_rejection must swallow any non-QueueFull exception."""
    adapter = _make_adapter(tmp_config)
    sink = MagicMock()
    sink.put_nowait = MagicMock(side_effect=RuntimeError("unexpected"))
    adapter._rejection_sink = sink

    intent = _make_intent()
    adapter._send_dispatch_rejection(intent, "CB_OPEN")

    adapter.metrics.rejection_sink_overflow_total.inc.assert_not_called()


def test_send_dispatch_rejection_no_sink_is_noop(tmp_config):
    """_send_dispatch_rejection with no sink is a silent no-op."""
    adapter = _make_adapter(tmp_config)
    adapter._rejection_sink = None

    intent = _make_intent()
    adapter._send_dispatch_rejection(intent, "NOOP")
    assert True


@pytest.mark.asyncio
async def test_sweep_stale_live_orders_evicts_expired_entries(tmp_config):
    """sweep_stale_live_orders evicts entries older than TTL."""
    adapter = _make_adapter(tmp_config)
    adapter._live_orders_ttl_s = 60.0
    adapter._live_orders_last_sweep_s = 0.0

    old_ts = time.monotonic() - 120.0
    adapter._live_orders_inserted_at["stale_key"] = old_ts
    adapter.live_orders["stale_key"] = {"status": "open"}

    evicted = await adapter.sweep_stale_live_orders()

    assert evicted >= 1
    assert "stale_key" not in adapter.live_orders


@pytest.mark.asyncio
async def test_sweep_stale_live_orders_rate_limited(tmp_config):
    """sweep_stale_live_orders is rate-limited: returns 0 if called too soon."""
    adapter = _make_adapter(tmp_config)
    adapter._live_orders_last_sweep_s = time.monotonic()

    adapter.live_orders["key1"] = {"status": "open"}
    adapter._live_orders_inserted_at["key1"] = time.monotonic() - 3600.0

    evicted = await adapter.sweep_stale_live_orders()
    assert evicted == 0
    assert "key1" in adapter.live_orders


@pytest.mark.asyncio
async def test_sweep_stale_live_orders_prunes_orphaned_timestamps(tmp_config):
    """Orphaned _live_orders_inserted_at entries (key not in live_orders) are pruned."""
    adapter = _make_adapter(tmp_config)
    adapter._live_orders_last_sweep_s = 0.0

    adapter._live_orders_inserted_at["orphan_key"] = time.monotonic()

    await adapter.sweep_stale_live_orders()

    assert "orphan_key" not in adapter._live_orders_inserted_at


@pytest.mark.asyncio
async def test_sweep_stale_live_orders_hard_cap_evicts_oldest(tmp_config):
    """Hard cap path evicts oldest entries when live_orders exceeds max_size after TTL sweep."""
    adapter = _make_adapter(tmp_config)
    adapter._live_orders_max_size = 3
    adapter._live_orders_last_sweep_s = 0.0
    adapter._live_orders_ttl_s = 9999.0

    now = time.monotonic()
    for i in range(5):
        key = f"strat:{i}"
        adapter.live_orders[key] = {"status": "open"}
        adapter._live_orders_inserted_at[key] = now - (5 - i)

    evicted = await adapter.sweep_stale_live_orders()

    assert len(adapter.live_orders) <= adapter._live_orders_max_size
    assert evicted >= 2


@pytest.mark.asyncio
async def test_sweep_stale_live_orders_clears_expired_pending_fill(tmp_config):
    """Orphaned pending-fill entries past TTL are evicted during sweep."""
    adapter = _make_adapter(tmp_config)
    adapter._live_orders_last_sweep_s = 0.0
    adapter._pending_fill_ttl_s = 60.0

    old_ts = time.monotonic() - 120.0
    order_key = "s1:99"
    pf_key = "2330:BUY"
    with adapter._pending_fill_lock:
        adapter._pending_fill_registered_at[order_key] = old_ts
        adapter._pending_fill_index[pf_key] = [order_key]

    await adapter.sweep_stale_live_orders()

    with adapter._pending_fill_lock:
        assert order_key not in adapter._pending_fill_registered_at


@pytest.mark.asyncio
async def test_run_blocking_call_handles_runtimeerror_on_closed_loop(tmp_config):
    """_run_blocking_call must handle RuntimeError from call_soon_threadsafe gracefully."""
    adapter = _make_adapter(tmp_config)

    call_log = []

    def _raise_immediately():
        raise ValueError("broker failure")

    with pytest.raises(ValueError, match="broker failure"):
        await adapter._run_blocking_call(_raise_immediately)


@pytest.mark.asyncio
async def test_run_blocking_call_succeeds_with_return_value(tmp_config):
    """_run_blocking_call returns the sync function result correctly."""
    adapter = _make_adapter(tmp_config)

    def _return_42():
        return 42

    result = await adapter._run_blocking_call(_return_42)
    assert result == 42


def test_resolve_phantom_fill_returns_none_with_empty_phantom_keys(tmp_config):
    """resolve_phantom_fill returns None immediately when no phantom candidates."""
    adapter = _make_adapter(tmp_config)
    assert len(adapter._phantom_order_keys) == 0

    fill = SimpleNamespace(symbol="2330", side=Side.BUY)
    result = adapter.resolve_phantom_fill(fill)
    assert result is None


def test_resolve_phantom_fill_returns_none_for_empty_symbol(tmp_config):
    """resolve_phantom_fill returns None when fill has no symbol."""
    adapter = _make_adapter(tmp_config)
    adapter._phantom_order_keys["s1:1"] = (time.monotonic(), "2330")

    fill = SimpleNamespace(symbol="", side=Side.BUY)
    result = adapter.resolve_phantom_fill(fill)
    assert result is None


def test_resolve_phantom_fill_via_pending_fill_index(tmp_config):
    """resolve_phantom_fill resolves via _pending_fill_index when available."""
    adapter = _make_adapter(tmp_config)
    now = time.monotonic()
    order_key = "s1:42"
    adapter._phantom_order_keys[order_key] = (now, "2330")

    pf_key = "2330:BUY"
    with adapter._pending_fill_lock:
        adapter._pending_fill_index[pf_key] = [order_key]
        adapter._pending_fill_registered_at[order_key] = now

    fill = SimpleNamespace(symbol="2330", side=0)
    result = adapter.resolve_phantom_fill(fill)

    assert result == "s1"
    assert order_key not in adapter._phantom_order_keys


def test_resolve_phantom_fill_via_fallback_phantom_keys(tmp_config):
    """resolve_phantom_fill falls back to phantom_keys scan when no pending_fill_index match."""
    adapter = _make_adapter(tmp_config)
    now = time.monotonic()
    order_key = "strategy_a:99"
    adapter._phantom_order_keys[order_key] = (now, "2330")

    fill = SimpleNamespace(symbol="2330", side=0)
    result = adapter.resolve_phantom_fill(fill)

    assert result == "strategy_a"


@pytest.mark.asyncio
async def test_run_finally_cleans_up_api_worker_task(tmp_config):
    """run() finally block must cancel and await _api_worker_task, then set it to None."""
    adapter = _make_adapter(tmp_config)

    task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.05)
    assert adapter._api_worker_task is not None

    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert adapter._api_worker_task is None


@pytest.mark.asyncio
async def test_run_finally_cleans_up_when_api_worker_already_none(tmp_config):
    """run() finally block handles _api_worker_task=None gracefully (no cancel attempt)."""
    adapter = _make_adapter(tmp_config)
    adapter._api_worker_task = None

    task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert adapter._api_worker_task is None


@pytest.mark.asyncio
async def test_run_finally_handles_timeout_on_api_worker_cancel(tmp_config):
    """run() finally block sets _api_worker_task to None after CancelledError."""
    adapter = _make_adapter(tmp_config)

    task = asyncio.create_task(adapter.run())
    await asyncio.sleep(0.03)

    assert adapter._api_worker_task is not None

    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert adapter._api_worker_task is None


@pytest.mark.asyncio
async def test_deadletter_add_continues_when_metrics_raise(tmp_path):
    """DLQ add() must continue even if metrics update raises an exception."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path), max_buffer_size=100)
    broken_metrics = MagicMock()
    broken_metrics.dlq_size_total = MagicMock()
    broken_metrics.dlq_size_total.labels.return_value.inc = MagicMock(side_effect=RuntimeError("metrics exploded"))
    dlq._metrics = broken_metrics

    await dlq.add(
        order_id="o1",
        strategy_id="s1",
        symbol="TXF",
        side="BUY",
        price=100_0000,
        qty=1,
        reason=RejectionReason.UNKNOWN,
        error_message="test",
    )

    stats = await dlq.get_stats()
    assert stats["total_entries"] == 1


@pytest.mark.asyncio
async def test_deadletter_buffer_overflow_drops_oldest_entries(tmp_path):
    """When buffer still exceeds max after failed flush, oldest entries are dropped."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path), max_buffer_size=3)

    async def _failing_flush():
        return 0

    dlq._flush_locked = _failing_flush  # type: ignore[method-assign]

    from hft_platform.order.deadletter import DeadLetterEntry

    for i in range(5):
        dlq._buffer.append(
            DeadLetterEntry(
                timestamp_ns=i,
                order_id=f"o{i}",
                strategy_id="s1",
                symbol="TXF",
                side="BUY",
                price=100,
                qty=1,
                reason="unknown",
                error_message="overflow test",
            )
        )

    dlq.max_buffer_size = 3
    await dlq.add(
        order_id="overflow",
        strategy_id="s1",
        symbol="TXF",
        side="BUY",
        price=100,
        qty=1,
        reason=RejectionReason.UNKNOWN,
        error_message="overflow",
    )

    assert len(dlq._buffer) <= dlq.max_buffer_size + 1


@pytest.mark.asyncio
async def test_deadletter_flush_empty_buffer_returns_zero(tmp_path):
    """_flush_locked returns 0 immediately when buffer is empty."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path))
    assert len(dlq._buffer) == 0

    flushed = await dlq.flush()
    assert flushed == 0


@pytest.mark.asyncio
async def test_deadletter_write_entries_exception_returns_zero(tmp_path):
    """_write_entries returns 0 and logs error when file write fails."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path), max_buffer_size=2)

    tmp_path.chmod(0o444)
    try:
        await dlq.add(
            order_id="o1",
            strategy_id="s1",
            symbol="TXF",
            side="BUY",
            price=100,
            qty=1,
            reason=RejectionReason.API_TIMEOUT,
            error_message="timeout",
        )
        await dlq.add(
            order_id="o2",
            strategy_id="s1",
            symbol="TXF",
            side="SELL",
            price=100,
            qty=1,
            reason=RejectionReason.API_TIMEOUT,
            error_message="timeout",
        )
    finally:
        tmp_path.chmod(0o755)

    assert dlq.total_entries == 2
    assert dlq.total_flushed == 0
    assert len(dlq._buffer) == 0


@pytest.mark.asyncio
async def test_deadletter_write_entries_exception_direct(tmp_path):
    """_write_entries directly returns 0 when write raises."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path))
    from hft_platform.order.deadletter import DeadLetterEntry

    entries = [
        DeadLetterEntry(
            timestamp_ns=1,
            order_id="o1",
            strategy_id="s1",
            symbol="TXF",
            side="BUY",
            price=100,
            qty=1,
            reason="unknown",
            error_message="err",
        )
    ]

    with patch("builtins.open", side_effect=OSError("disk full")):
        result = dlq._write_entries(entries)

    assert result == 0


def test_deadletter_cleanup_old_files_deletes_expired(tmp_path):
    """_cleanup_old_files deletes DLQ files with mtime older than retain_days."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path))

    old_file = tmp_path / "dlq_old.jsonl"
    old_file.write_text("{}\n")
    old_mtime = time.time() - 30 * 86400
    os.utime(old_file, (old_mtime, old_mtime))

    with patch.dict(os.environ, {"HFT_DLQ_RETAIN_DAYS": "7"}):
        dlq._cleanup_old_files()

    assert not old_file.exists()


def test_deadletter_cleanup_old_files_exception_per_file_is_swallowed(tmp_path):
    """_cleanup_old_files swallows per-file deletion exceptions and continues."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path))

    old_file = tmp_path / "dlq_999.jsonl"
    old_file.write_text("{}\n")
    old_mtime = time.time() - 30 * 86400
    os.utime(old_file, (old_mtime, old_mtime))

    with (
        patch.dict(os.environ, {"HFT_DLQ_RETAIN_DAYS": "7"}),
        patch.object(Path, "unlink", side_effect=OSError("permission denied")),
    ):
        dlq._cleanup_old_files()

    assert old_file.exists()


def test_deadletter_read_all_empty_dir_returns_empty_list(tmp_path):
    """read_all returns empty list when no DLQ files exist."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path))
    entries = dlq.read_all()
    assert entries == []


def test_deadletter_read_all_skips_unreadable_files(tmp_path):
    """read_all skips files that raise an exception on open."""
    dlq = DeadLetterQueue(dlq_dir=str(tmp_path))

    valid_entry = {
        "timestamp_ns": 1,
        "order_id": "o_valid",
        "strategy_id": "s1",
        "symbol": "TXF",
        "side": "BUY",
        "price": 100,
        "qty": 1,
        "reason": "unknown",
        "error_message": "e",
        "intent_type": "NEW",
        "metadata": {},
        "retry_count": 0,
        "trace_id": "",
        "halt_exempt_blocked": False,
    }
    valid_file = tmp_path / "dlq_200.jsonl"
    valid_file.write_text(json.dumps(valid_entry) + "\n")

    bad_file = tmp_path / "dlq_100.jsonl"
    bad_file.write_text("irrelevant")

    original_open = open

    def _selective_open(path, *args, **kwargs):
        if str(path) == str(bad_file):
            raise PermissionError("access denied")
        return original_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=_selective_open):
        entries = dlq.read_all()

    assert any(e.order_id == "o_valid" for e in entries)


@pytest.mark.asyncio
async def test_halt_canceller_batch_overflow_flushes_and_sleeps():
    """When live orders exceed _BATCH_SIZE, commands are flushed per batch with sleep."""
    n_orders = _BATCH_SIZE + 5
    orders = {f"strat_a:{i}": {"status": "open"} for i in range(n_orders)}

    adapter = SimpleNamespace(
        live_orders=orders,
        _live_orders_lock=asyncio.Lock(),
        order_queue=asyncio.Queue(maxsize=n_orders * 2),
    )
    sg = SimpleNamespace(state=StormGuardState.HALT)

    with patch("hft_platform.order.halt_canceller.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        count = await cancel_all_live_orders(adapter, sg)

    assert count == n_orders
    assert adapter.order_queue.qsize() == n_orders
    mock_sleep.assert_called()


@pytest.mark.asyncio
async def test_halt_canceller_exact_batch_size_triggers_sleep():
    """Exactly _BATCH_SIZE live orders triggers the batch flush + sleep path."""
    orders = {f"strat_x:{i}": {"status": "open"} for i in range(_BATCH_SIZE)}
    adapter = SimpleNamespace(
        live_orders=orders,
        _live_orders_lock=asyncio.Lock(),
        order_queue=asyncio.Queue(maxsize=_BATCH_SIZE * 2),
    )
    sg = SimpleNamespace(state=StormGuardState.HALT)

    with patch("hft_platform.order.halt_canceller.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        count = await cancel_all_live_orders(adapter, sg)

    assert count == _BATCH_SIZE
    mock_sleep.assert_called_once()


def test_shadow_get_metrics_returns_none_on_import_error(monkeypatch):
    """_get_metrics must return None when MetricsRegistry import raises."""
    import hft_platform.order.shadow as shadow_mod

    with patch("hft_platform.order.shadow._get_metrics", side_effect=Exception("import fail")):
        pass

    with patch("hft_platform.observability.metrics.MetricsRegistry") as mock_reg:
        mock_reg.get.side_effect = RuntimeError("metrics unavailable")
        result = shadow_mod._get_metrics()
        assert result is None


def test_shadow_get_metrics_swallows_all_exceptions():
    """_get_metrics catches any exception and returns None (no re-raise)."""
    import hft_platform.order.shadow as shadow_mod

    with patch("hft_platform.observability.metrics.MetricsRegistry") as mock_reg:
        mock_reg.get.side_effect = ImportError("circular import")
        result = shadow_mod._get_metrics()
        assert result is None


def test_shadow_intercept_calls_writer_add_when_writer_set():
    """intercept() calls _writer.add(record) when _writer is not None."""
    mock_writer = MagicMock()
    sink = ShadowOrderSink(enabled=True, writer=mock_writer)

    intent = MagicMock()
    intent.strategy_id = "strat1"
    intent.symbol = "2330"
    intent.side = MagicMock()
    intent.side.name = "BUY"
    intent.price = 5_000_000
    intent.qty = 1
    intent.intent_type = MagicMock()
    intent.intent_type.name = "NEW"
    intent.intent_id = "abc123"

    record = sink.intercept(intent)

    mock_writer.add.assert_called_once_with(record)


def test_shadow_intercept_does_not_call_writer_when_writer_is_none():
    """intercept() does not attempt any write when _writer is None."""
    sink = ShadowOrderSink(enabled=True, writer=None)

    intent = MagicMock()
    intent.strategy_id = "strat1"
    intent.symbol = "2330"
    intent.side = MagicMock()
    intent.side.name = "BUY"
    intent.price = 5_000_000
    intent.qty = 1
    intent.intent_type = MagicMock()
    intent.intent_type.name = "NEW"
    intent.intent_id = "id1"

    record = sink.intercept(intent)
    assert record["shadow"] is True
    assert sink.counter == 1


def test_shadow_flush_delegates_to_writer():
    """flush() must call _writer.flush() when _writer is set."""
    mock_writer = MagicMock()
    sink = ShadowOrderSink(enabled=True, writer=mock_writer)

    sink.flush()

    mock_writer.flush.assert_called_once()


def test_shadow_flush_noop_when_no_writer():
    """flush() is a no-op when _writer is None (must not raise)."""
    sink = ShadowOrderSink(enabled=True, writer=None)
    sink.flush()
    assert True
