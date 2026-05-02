"""Tests for P0-A: Exposure release and dedup ordering correctness on QueueFull.

Covers two bugs fixed in gateway/service.py:
  C1: Exposure was not released when api_queue raised QueueFull.
  C2: Dedup was committed approved=True before dispatch, causing TOCTOU corruption.
"""

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskDecision,
    Side,
    StormGuardState,
)
from hft_platform.gateway.channel import LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureKey, ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService


def _make_intent(
    intent_id: int = 1,
    key: str = "k1",
    intent_type: IntentType = IntentType.NEW,
    symbol: str = "TSE:2330",
    price: int = 1_000_000,
    qty: int = 1,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        idempotency_key=key,
    )


def _make_service(
    channel: LocalIntentChannel | None = None,
    approve: bool = True,
    queue_full: bool = False,
    exposure_store: ExposureStore | None = None,
    dedup_store: IdempotencyStore | None = None,
) -> tuple[GatewayService, asyncio.Queue]:
    if channel is None:
        channel = LocalIntentChannel(maxsize=64, ttl_ms=0)

    risk_engine = MagicMock()
    risk_engine.evaluate.return_value = RiskDecision(
        approved=approve,
        intent=MagicMock(),
        reason_code="OK" if approve else "TEST_REJECT",
    )
    cmd = OrderCommand(
        cmd_id=1,
        intent=MagicMock(),
        deadline_ns=999,
        storm_guard_state=StormGuardState.NORMAL,
    )
    risk_engine.create_command.return_value = cmd

    api_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    if queue_full:
        for _ in range(64):
            api_queue.put_nowait(MagicMock())

    order_adapter = MagicMock()
    order_adapter._api_queue = api_queue
    order_adapter._supports_typed_command_ingress = False

    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL

    svc = GatewayService(
        channel=channel,
        risk_engine=risk_engine,
        order_adapter=order_adapter,
        exposure_store=exposure_store if exposure_store is not None else ExposureStore(),
        dedup_store=dedup_store if dedup_store is not None else IdempotencyStore(persist_enabled=False),
        storm_guard=storm_guard,
        policy=GatewayPolicy(),
    )
    return svc, api_queue


async def _run_one_intent(svc: GatewayService, ch: LocalIntentChannel, intent: OrderIntent) -> None:
    """Submit one intent and run the service loop until it is consumed."""
    ch.submit_nowait(intent)
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ── C1: Exposure release on QueueFull ────────────────────────────────────────


@pytest.mark.asyncio
async def test_queuefull_releases_exposure():
    """C1: Exposure reserved in Step 3 must be released when api_queue raises QueueFull."""
    # Use a tight notional cap so we can verify re-admission after release
    exposure = ExposureStore(global_max_notional=1_000_000)
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch, queue_full=True, exposure_store=exposure)

    intent = _make_intent(key="k_exp_release", price=1_000_000, qty=1)
    await _run_one_intent(svc, ch, intent)

    # Intent was rejected by QueueFull
    assert svc._rejected == 1
    assert svc._dispatched == 0

    # After the failed dispatch, exposure must have been released so that the
    # *same* notional can be reserved again on a retry.
    exp_key = ExposureKey(account="default", strategy_id="s1", symbol="TSE:2330")
    ok, reason = exposure.check_and_update(exp_key, intent)
    assert ok is True, (
        f"Exposure was not released on QueueFull: second check_and_update returned ok={ok}, reason={reason}"
    )


@pytest.mark.asyncio
async def test_queuefull_does_not_release_exposure_for_cancel():
    """C1 edge-case: CANCEL intents skip exposure tracking — no release should occur (no crash)."""
    exposure = ExposureStore(global_max_notional=1_000_000)
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch, queue_full=True, exposure_store=exposure)

    intent = _make_intent(key="k_cancel_full", intent_type=IntentType.CANCEL)
    await _run_one_intent(svc, ch, intent)

    # Should still be rejected (queue full), just no exposure to release
    assert svc._rejected == 1


# ── C2: Dedup commit ordering ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queuefull_commits_dedup_as_rejected():
    """C2: Dedup entry must be committed approved=False with reason ORDER_QUEUE_FULL on QueueFull."""
    dedup = IdempotencyStore(persist_enabled=False)
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch, queue_full=True, dedup_store=dedup)

    intent = _make_intent(key="k_dedup_full")
    await _run_one_intent(svc, ch, intent)

    rec = dedup.check_or_reserve("k_dedup_full")
    assert rec is not None, "Dedup record must exist after QueueFull rejection"
    assert rec.approved is False, f"Expected approved=False, got {rec.approved}"
    assert rec.reason_code == "ORDER_QUEUE_FULL", f"Expected ORDER_QUEUE_FULL, got {rec.reason_code}"


@pytest.mark.asyncio
async def test_successful_dispatch_commits_dedup_as_approved():
    """C2: Dedup entry must be committed approved=True only after a successful dispatch."""
    dedup = IdempotencyStore(persist_enabled=False)
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, queue_full=False, dedup_store=dedup)

    intent = _make_intent(key="k_dedup_ok")
    await _run_one_intent(svc, ch, intent)

    assert svc._dispatched == 1
    assert api_queue.qsize() == 1

    rec = dedup.check_or_reserve("k_dedup_ok")
    assert rec is not None, "Dedup record must exist after successful dispatch"
    assert rec.approved is True, f"Expected approved=True, got {rec.approved}"
    assert rec.reason_code == "OK", f"Expected OK, got {rec.reason_code}"


@pytest.mark.asyncio
async def test_queuefull_then_normal_dispatch_allowed():
    """C2 integration: after a QueueFull rejection the key is marked rejected,
    and a fresh key on an empty queue dispatches successfully."""
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc_full, _ = _make_service(channel=ch, queue_full=True)

    # First intent hits QueueFull
    intent_full = _make_intent(key="k_first_full")
    await _run_one_intent(svc_full, ch, intent_full)
    assert svc_full._rejected == 1

    # New service instance (empty queue) with a different key dispatches fine
    ch2 = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc_ok, api_queue2 = _make_service(channel=ch2, queue_full=False)
    intent_ok = _make_intent(key="k_second_ok")
    await _run_one_intent(svc_ok, ch2, intent_ok)
    assert svc_ok._dispatched == 1
    assert api_queue2.qsize() == 1
