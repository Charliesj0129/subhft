"""P1-4: _api_worker must release dedup slots and clear _api_pending on
cancellation. Previously, the outer ``except Exception`` did not catch
``asyncio.CancelledError``, so a shutdown mid-coalesce-window (item
already materialised into ``_api_pending`` but not yet dispatched) would
leak the dedup reservation; on a subsequent restart the same
idempotency key would be rejected as "in-flight duplicate".
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.order.adapter import OrderAdapter


class _MockBrokerClient:
    mode = "simulation"
    activate_ca = False
    ca_active = False

    def get_exchange(self, symbol: str) -> str:  # pragma: no cover
        return "TSE"


def _make_adapter(tmp_path) -> OrderAdapter:
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n  threshold: 5\n  timeout_seconds: 60\n"
    )
    os.environ["HFT_ORDER_ID_MAP_PERSIST_PATH"] = str(tmp_path / "order_id_map.jsonl")
    queue: asyncio.Queue[Any] = asyncio.Queue()
    return OrderAdapter(
        config_path=str(cfg),
        order_queue=queue,
        broker_client=_MockBrokerClient(),
    )


def _cmd(sid: str, iid: int, idem: str = "") -> OrderCommand:
    intent = OrderIntent(
        intent_id=iid,
        strategy_id=sid,
        symbol="TMFD6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=500_0000,
        qty=1,
        tif=TIF.ROD,
        idempotency_key=idem,
    )
    return OrderCommand(
        cmd_id=iid,
        intent=intent,
        deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=0,
    )


@pytest.mark.asyncio
async def test_release_pending_on_cancel_clears_and_releases(tmp_path):
    """Direct unit test of the helper: a non-empty _api_pending must be
    fully released on cancel. The helper must never raise."""
    adapter = _make_adapter(tmp_path)
    # Seed _api_pending manually.
    c1 = _cmd("S1", 1, idem="idem-1")
    c2 = _cmd("S2", 2, idem="idem-2")
    adapter._api_pending[("new", "S1", "TMFD6", 1)] = c1
    adapter._api_pending[("new", "S2", "TMFD6", 2)] = c2

    adapter._release_pending_on_cancel()

    assert adapter._api_pending == {}


@pytest.mark.asyncio
async def test_api_worker_cancellation_clears_api_pending(tmp_path):
    """End-to-end: start _api_worker, push one cmd, cancel the task while
    it is in the coalesce window. After cancel, _api_pending must be empty."""
    adapter = _make_adapter(tmp_path)
    adapter.running = True
    # Use a broker client that blocks forever so the dispatch coroutine
    # does not consume the item before we cancel.

    class _BlockingClient(_MockBrokerClient):
        def place_order(self, *a, **kw):  # pragma: no cover
            import time

            time.sleep(10)

    adapter.client = _BlockingClient()
    # Force a non-trivial coalesce window so the worker sits in wait_for.
    adapter._api_coalesce_window_s = 5.0

    # Push one NEW cmd so the worker materialises and stores it.
    cmd = _cmd("S1", 42, idem="idem-42")
    adapter._api_queue.put_nowait(cmd)

    worker = asyncio.create_task(adapter._api_worker())

    # Let the worker enter the coalesce window (it will have the first
    # item stored in _api_pending and be awaiting a second inside
    # ``asyncio.wait_for(get, timeout=remaining)``).
    await asyncio.sleep(0.05)
    assert len(adapter._api_pending) == 1, "precondition: one cmd pending"

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert adapter._api_pending == {}, "P1-4 regression: _api_pending not cleared on cancel"


@pytest.mark.asyncio
async def test_api_worker_cancel_on_initial_get_is_safe(tmp_path):
    """Cancel while the worker is awaiting the first `get()` (no items
    materialised yet). Must not raise, _api_pending remains empty."""
    adapter = _make_adapter(tmp_path)
    adapter.running = True

    worker = asyncio.create_task(adapter._api_worker())
    await asyncio.sleep(0.05)
    assert adapter._api_pending == {}

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert adapter._api_pending == {}


@pytest.mark.asyncio
async def test_api_worker_cancel_during_dispatch_releases_dedup(tmp_path):
    """P1-4 hole (Codex stop-time review): the original P1-4 fix only
    releases dedup slots from ``_api_pending``. But ``_api_pending`` is
    cleared *before* the dispatch loop starts (``_api_pending.clear()``
    immediately after the snapshot). If ``CancelledError`` fires during
    ``await self._dispatch_to_api(item)``, the outer cancel handler runs
    against an already-empty ``_api_pending`` and the un-dispatched portion
    of the local ``pending`` list leaks dedup reservations.

    Fix: track the snapshot in ``_api_inflight`` so cancel + exception
    handlers can drain it.
    """
    adapter = _make_adapter(tmp_path)
    adapter.running = True
    adapter._api_coalesce_window_s = 0.0  # skip coalesce window — go straight to dispatch

    started = asyncio.Event()
    block = asyncio.Event()  # never set → dispatch hangs

    async def hanging_dispatch(cmd):
        started.set()
        await block.wait()
        return True

    adapter._dispatch_to_api = hanging_dispatch

    # Reserve the dedup slot so we can detect a leak. In production this
    # happens at the gateway entry (line 1670). For the test we reserve
    # directly on the store.
    assert adapter._dedup_store is not None
    adapter._dedup_store.check_or_reserve("idem-mid-99")
    assert "idem-mid-99" in adapter._dedup_store._records

    cmd = _cmd("S1", 99, idem="idem-mid-99")
    adapter._api_queue.put_nowait(cmd)

    worker = asyncio.create_task(adapter._api_worker())

    await asyncio.wait_for(started.wait(), timeout=1.0)
    # Precondition: _api_pending was cleared before dispatch started.
    assert adapter._api_pending == {}, "precondition: _api_pending was cleared before dispatch"

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    # Regression assertion: the un-dispatched portion of `pending` must
    # have its dedup slot released, even though _api_pending was already
    # empty when cancel fired.
    assert "idem-mid-99" not in adapter._dedup_store._records, (
        "P1-4 hole: cancel during _dispatch_to_api await leaked dedup slot; "
        "_release_pending_on_cancel must also drain _api_inflight"
    )
