"""A cancel must not be dropped by the command deadline, and a dropped or failed
cancel must not release a pending slot it never took.

THESHOW, R47 (SIM), 2026-10-05: six SELL orders were never cancelled. R47 sends
CANCEL BUY then CANCEL SELL in one pass. The BUY cancel hit three 3 s broker
timeouts; the SELL cancel waited behind it, outlived its deadline and was dropped
by ``_api_worker`` (``_add_to_dlq(DEADLINE_EXCEEDED)``). R47 had already forgotten
the SELL order id, so nothing ever cancelled it again, and the order sat live
until the 300 s TTL sweep.

Two defects, one per test group:

* A1 -- ``CANCEL`` and ``FORCE_FLAT`` reduce risk; a late one is still the right
  command (a target that already ended is handled by ``cancel_already_terminal``).
  ``NEW`` and ``AMEND`` still expire: their price is stale.
* A2 -- the dispatch-failure ``RiskFeedback`` carries ``side`` and
  ``strategy.base.cancel`` builds every CANCEL with ``side=Side.BUY``, so a
  failed SELL cancel released ``_pending_buy`` (R47 decrements purely on
  ``feedback.side``). Only a ``NEW`` ever took a pending slot.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, RiskFeedback, Side, StormGuardState
from hft_platform.core import timebase

_SID = "strat1"


class _StubCodec:
    def encode_side(self, side):
        return "Buy"

    def encode_tif(self, tif):
        return "IOC"

    def encode_price_type(self, price_type):
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
        mm.get.return_value = metrics_mock
        ml.get.return_value = MagicMock()
        md.return_value = AsyncMock()
        mp.return_value = MagicMock()
        meta_inst = MagicMock()
        meta_inst.exchange.return_value = "TAIFEX"
        meta_inst.product_type.return_value = None
        meta_inst.order_params.return_value = {}
        ms.return_value = meta_inst
        yield metrics_mock


def _adapter(tmp_config: str):
    from hft_platform.order.adapter import OrderAdapter

    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock())
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.get_exchange = MagicMock(return_value="TAIFEX")
    client.mode = "simulation"
    client.activate_ca = False
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(),
        broker_client=client,
        broker_codec=_StubCodec(),
    )
    adapter.shadow_sink.enabled = False
    adapter._audit_writer = MagicMock()
    adapter.set_rejection_sink(asyncio.Queue(maxsize=16))
    return adapter


def _cmd(intent_type: IntentType, *, side: Side = Side.BUY, intent_id: int = 7, expired: bool = True) -> OrderCommand:
    intent = OrderIntent(
        intent_id=intent_id,
        strategy_id=_SID,
        symbol="TMFJ6",
        intent_type=intent_type,
        side=side,
        price=0 if intent_type == IntentType.CANCEL else 5_000_000,
        qty=0 if intent_type == IntentType.CANCEL else 1,
        target_order_id="OID-1" if intent_type == IntentType.CANCEL else "",
        reason="",
    )
    # ``deadline_ns`` lives in the monotonic domain (see _intent_to_command).
    deadline = time.monotonic_ns() - 1_000_000 if expired else time.monotonic_ns() + 10**10
    return OrderCommand(
        cmd_id=intent_id,
        intent=intent,
        deadline_ns=deadline,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


def _feedbacks(adapter) -> list[RiskFeedback]:
    sink = adapter._rejection_sink
    out = []
    while not sink.empty():
        out.append(sink.get_nowait())
    return out


async def _run_loop_once(adapter, cmd: OrderCommand) -> None:
    """Feed one command through ``run()``'s pre-dispatch deadline check."""
    adapter.execute = AsyncMock()
    adapter.order_queue.put_nowait(cmd)
    task = asyncio.create_task(adapter.run())
    await asyncio.wait_for(adapter.order_queue.join(), timeout=5.0)  # run() calls task_done() after each command
    adapter.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _run_worker_once(adapter, cmd: OrderCommand) -> AsyncMock:
    """Feed one command through ``_api_worker``'s deadline check."""
    adapter.running = True
    adapter._api_coalesce_window_s = 0.0
    dispatch = AsyncMock(return_value=True)
    adapter._dispatch_to_api = dispatch
    adapter._api_queue.put_nowait(cmd)
    worker = asyncio.create_task(adapter._api_worker())
    # Done = the command left the queue, the coalesce buffer and the in-flight list.
    for _ in range(1000):
        if adapter._api_queue.empty() and not adapter._api_pending and not adapter._api_inflight:
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("_api_worker never drained the command")
    adapter.running = False
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    return dispatch


# ── A1: run() pre-dispatch check ──────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("intent_type", [IntentType.CANCEL, IntentType.FORCE_FLAT])
async def test_risk_reducing_command_past_deadline_is_still_executed_by_run(tmp_config, intent_type):
    adapter = _adapter(tmp_config)
    cmd = _cmd(intent_type)

    await _run_loop_once(adapter, cmd)

    adapter.execute.assert_awaited_once_with(cmd)
    adapter._dlq.add.assert_not_awaited()
    assert _feedbacks(adapter) == []
    # Counted once, where the command reaches the broker call (_api_worker) -- not here too.
    adapter.metrics.order_deadline_overridden_total.labels.assert_not_called()


@pytest.mark.asyncio
async def test_new_past_deadline_is_still_dropped_by_run_and_releases_its_own_slot(tmp_config):
    adapter = _adapter(tmp_config)
    cmd = _cmd(IntentType.NEW, side=Side.SELL)

    await _run_loop_once(adapter, cmd)

    adapter.execute.assert_not_awaited()
    adapter._dlq.add.assert_awaited_once()
    (fb,) = _feedbacks(adapter)
    assert fb.reason_code == "dispatch_deadline_expired"
    assert fb.side == Side.SELL
    assert fb.was_approved is False


@pytest.mark.asyncio
async def test_amend_past_deadline_is_dropped_without_releasing_a_slot_it_never_took(tmp_config):
    adapter = _adapter(tmp_config)
    cmd = _cmd(IntentType.AMEND, side=Side.SELL)

    await _run_loop_once(adapter, cmd)

    adapter.execute.assert_not_awaited()
    assert _feedbacks(adapter) == []


# ── A1: _api_worker check ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("intent_type", [IntentType.CANCEL, IntentType.FORCE_FLAT])
async def test_risk_reducing_command_past_deadline_is_still_dispatched_by_api_worker(tmp_config, intent_type):
    adapter = _adapter(tmp_config)
    cmd = _cmd(intent_type)

    dispatch = await _run_worker_once(adapter, cmd)

    dispatch.assert_awaited_once()
    adapter._dlq.add.assert_not_awaited()
    assert _feedbacks(adapter) == []


@pytest.mark.asyncio
async def test_late_cancel_is_counted_so_the_override_is_visible(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)

    await _run_worker_once(adapter, _cmd(IntentType.CANCEL))

    mock_deps.order_deadline_overridden_total.labels.assert_called_with(intent_type="CANCEL")


@pytest.mark.asyncio
async def test_new_past_deadline_is_still_dropped_by_api_worker_and_releases_its_own_slot(tmp_config):
    adapter = _adapter(tmp_config)
    cmd = _cmd(IntentType.NEW, side=Side.SELL)

    dispatch = await _run_worker_once(adapter, cmd)

    dispatch.assert_not_awaited()
    adapter._dlq.add.assert_awaited_once()
    (fb,) = _feedbacks(adapter)
    assert fb.reason_code == "dispatch_deadline_expired"
    assert fb.side == Side.SELL


@pytest.mark.asyncio
async def test_amend_past_deadline_is_dropped_by_api_worker_without_a_release(tmp_config):
    adapter = _adapter(tmp_config)

    dispatch = await _run_worker_once(adapter, _cmd(IntentType.AMEND, side=Side.SELL))

    dispatch.assert_not_awaited()
    assert _feedbacks(adapter) == []


@pytest.mark.asyncio
async def test_a_future_deadline_cancel_is_dispatched_without_the_override_count(tmp_config, mock_deps):
    adapter = _adapter(tmp_config)

    dispatch = await _run_worker_once(adapter, _cmd(IntentType.CANCEL, expired=False))

    dispatch.assert_awaited_once()
    mock_deps.order_deadline_overridden_total.labels.assert_not_called()


# ── A2: dispatch exception ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
async def test_cancel_dispatch_exception_does_not_release_a_pending_slot(tmp_config, side):
    adapter = _adapter(tmp_config)
    cmd = _cmd(IntentType.CANCEL, side=side)

    await adapter._handle_dispatch_exception(intent=cmd.intent, cmd_id=cmd.cmd_id)

    assert _feedbacks(adapter) == []


@pytest.mark.asyncio
async def test_amend_dispatch_exception_does_not_release_a_pending_slot(tmp_config):
    adapter = _adapter(tmp_config)
    cmd = _cmd(IntentType.AMEND, side=Side.SELL)

    await adapter._handle_dispatch_exception(intent=cmd.intent, cmd_id=cmd.cmd_id)

    assert _feedbacks(adapter) == []


@pytest.mark.asyncio
async def test_new_dispatch_exception_still_reports_a_phantom_candidate(tmp_config):
    adapter = _adapter(tmp_config)
    cmd = _cmd(IntentType.NEW, side=Side.SELL, expired=False)

    await adapter._handle_dispatch_exception(intent=cmd.intent, cmd_id=cmd.cmd_id)

    (fb,) = _feedbacks(adapter)
    assert fb.reason_code == "dispatch_failed"
    assert fb.side == Side.SELL
    assert fb.was_approved is True, "phantom: the order may be live, so the strategy keeps its slot"
