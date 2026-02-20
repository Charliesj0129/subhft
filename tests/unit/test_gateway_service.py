"""Tests for CE2-03: GatewayService."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, RiskDecision, Side, StormGuardState, TIF
from hft_platform.gateway.channel import LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService


def _make_intent(intent_id: int = 1, key: str = "k1", intent_type: IntentType = IntentType.NEW) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol="TSE:2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=1_000_000,
        qty=1,
        tif=TIF.LIMIT,
        idempotency_key=key,
    )


def _make_service(channel=None, approve=True, queue_full=False):
    if channel is None:
        channel = LocalIntentChannel(maxsize=64, ttl_ms=0)

    risk_engine = MagicMock()
    risk_engine.evaluate.return_value = RiskDecision(approved=approve, intent=MagicMock(), reason_code="OK" if approve else "TEST_REJECT")

    cmd = OrderCommand(cmd_id=1, intent=MagicMock(), deadline_ns=999, storm_guard_state=StormGuardState.NORMAL)
    risk_engine.create_command.return_value = cmd

    api_queue = asyncio.Queue(maxsize=64)
    if queue_full:
        for _ in range(64):
            api_queue.put_nowait(MagicMock())
    order_adapter = MagicMock()
    order_adapter._api_queue = api_queue

    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL

    svc = GatewayService(
        channel=channel,
        risk_engine=risk_engine,
        order_adapter=order_adapter,
        exposure_store=ExposureStore(),
        dedup_store=IdempotencyStore(persist_enabled=False),
        storm_guard=storm_guard,
        policy=GatewayPolicy(),
    )
    return svc, api_queue


@pytest.mark.asyncio
async def test_service_dispatches_approved_intent():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)

    intent = _make_intent(1, "k1")
    ch.submit_nowait(intent)

    # Run one iteration
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert api_queue.qsize() == 1
    health = svc.get_health()
    assert health["dispatched"] == 1
    assert health["rejected"] == 0


@pytest.mark.asyncio
async def test_service_rejected_by_risk():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=False)

    intent = _make_intent(1, "k1")
    ch.submit_nowait(intent)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert api_queue.qsize() == 0
    assert svc._rejected == 1


@pytest.mark.asyncio
async def test_service_dedup_hit_does_not_redispatch():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)

    for _ in range(3):
        ch.submit_nowait(_make_intent(1, "same-key"))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # First intent dispatched; 2 dedup hits
    assert api_queue.qsize() == 1
    assert svc._dedup_hits == 2


@pytest.mark.asyncio
async def test_service_get_health_keys():
    svc, _ = _make_service()
    health = svc.get_health()
    required_keys = {"running", "dispatched", "rejected", "dedup_hits", "channel_depth", "policy_mode"}
    assert required_keys.issubset(health.keys())


@pytest.mark.asyncio
async def test_service_cancelled_error_stops_loop():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, _ = _make_service(channel=ch)

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert svc.running is False


@pytest.mark.asyncio
async def test_service_halt_policy_blocks_new():
    ch = LocalIntentChannel(maxsize=64, ttl_ms=0)
    svc, api_queue = _make_service(channel=ch, approve=True)
    svc._policy.set_halt()
    svc._storm_guard.state = StormGuardState.HALT

    ch.submit_nowait(_make_intent(1, "k-halt", IntentType.NEW))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert api_queue.qsize() == 0
    assert svc._rejected >= 1
