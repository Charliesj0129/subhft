"""CE2-08: Integration test — multiple StrategyRunners + GatewayService.

Tests:
- 3 runner coroutines submit 50 intents each → 150 unique dispatches.
- Retry same idempotency_key 3× → 1 dispatch, 2 dedup hits.
- storm_guard HALT → no NEW dispatches; CANCEL still passes (policy allows).
- submit_nowait when channel full → QueueFull propagated.
- active/standby gateway HA lease failover avoids duplicate broker dispatches.
"""
import asyncio
import os
import tempfile
from contextlib import suppress
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, RiskDecision, Side, StormGuardState, TIF
from hft_platform.gateway.channel import LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService


def _make_intent(intent_id: int, key: str, intent_type: IntentType = IntentType.NEW) -> OrderIntent:
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


def _build_service(channel, api_queue, storm_guard=None):
    risk_engine = MagicMock()

    def evaluate(intent):
        return RiskDecision(approved=True, intent=intent, reason_code="OK")

    def create_command(intent):
        return OrderCommand(
            cmd_id=abs(hash(intent.idempotency_key)) % 100000,
            intent=intent,
            deadline_ns=9_999_999_999_999,
            storm_guard_state=StormGuardState.NORMAL,
        )

    risk_engine.evaluate.side_effect = evaluate
    risk_engine.create_command.side_effect = create_command

    order_adapter = MagicMock()
    order_adapter._api_queue = api_queue

    if storm_guard is None:
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
    return svc


@pytest.mark.asyncio
async def test_three_runners_150_unique_dispatches():
    """3 runners × 50 unique intents → 150 dispatched, 0 dedup hits."""
    channel = LocalIntentChannel(maxsize=512, ttl_ms=0)
    api_queue = asyncio.Queue(maxsize=512)
    svc = _build_service(channel, api_queue)

    async def runner(worker_id: int, n: int):
        for i in range(n):
            key = f"w{worker_id}-i{i}"
            channel.submit_nowait(_make_intent(worker_id * 1000 + i, key))
            await asyncio.sleep(0)

    task = asyncio.create_task(svc.run())
    await asyncio.gather(runner(0, 50), runner(1, 50), runner(2, 50))
    # Allow service to drain
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert svc._dispatched == 150
    assert svc._dedup_hits == 0


@pytest.mark.asyncio
async def test_dedup_same_key_3x():
    """Same idempotency_key 3 times → 1 dispatch, 2 dedup hits."""
    channel = LocalIntentChannel(maxsize=64, ttl_ms=0)
    api_queue = asyncio.Queue(maxsize=64)
    svc = _build_service(channel, api_queue)

    for _ in range(3):
        channel.submit_nowait(_make_intent(1, "shared-key"))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert svc._dispatched == 1
    assert svc._dedup_hits == 2


@pytest.mark.asyncio
async def test_halt_blocks_new_allows_cancel():
    """HALT state: NEW blocked, CANCEL allowed (HFT_GATEWAY_HALT_CANCEL=1 default)."""
    channel = LocalIntentChannel(maxsize=64, ttl_ms=0)
    api_queue = asyncio.Queue(maxsize=64)

    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.HALT
    svc = _build_service(channel, api_queue, storm_guard=storm_guard)
    svc._policy.set_halt()

    channel.submit_nowait(_make_intent(1, "new-1", IntentType.NEW))
    channel.submit_nowait(_make_intent(2, "cancel-1", IntentType.CANCEL))

    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # NEW should be rejected, CANCEL dispatched
    assert svc._rejected >= 1
    assert svc._dispatched == 1


@pytest.mark.asyncio
async def test_queue_full_raises():
    """submit_nowait when channel full → QueueFull propagated to caller."""
    channel = LocalIntentChannel(maxsize=2, ttl_ms=0)
    api_queue = asyncio.Queue(maxsize=64)
    svc = _build_service(channel, api_queue)

    channel.submit_nowait(_make_intent(1, "k1"))
    channel.submit_nowait(_make_intent(2, "k2"))

    with pytest.raises(asyncio.QueueFull):
        channel.submit_nowait(_make_intent(3, "k3"))


@pytest.mark.asyncio
async def test_gateway_ha_failover_no_duplicate_dispatch():
    """CE2-08/CE2-09: two gateways process duplicated inputs; only leader dispatches, standby takes over after outage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lease_path = os.path.join(tmpdir, "gateway.leader.lock")
        with patch.dict(
            os.environ,
            {
                "HFT_GATEWAY_HA_ENABLED": "1",
                "HFT_GATEWAY_LEADER_LEASE_PATH": lease_path,
                "HFT_GATEWAY_LEADER_LEASE_REFRESH_S": "0.05",
                "HFT_GATEWAY_METRICS": "0",
            },
            clear=False,
        ):
            ch1 = LocalIntentChannel(maxsize=1024, ttl_ms=0)
            ch2 = LocalIntentChannel(maxsize=1024, ttl_ms=0)
            shared_api_queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
            svc1 = _build_service(ch1, shared_api_queue)
            svc2 = _build_service(ch2, shared_api_queue)

            # Deterministic cmd_id for duplicate detection across gateways.
            for svc in (svc1, svc2):
                def _create_command(intent):
                    return OrderCommand(
                        cmd_id=int(intent.intent_id),
                        intent=intent,
                        deadline_ns=9_999_999_999_999,
                        storm_guard_state=StormGuardState.NORMAL,
                    )

                svc._risk_engine.create_command.side_effect = _create_command

            t1 = asyncio.create_task(svc1.run())
            t2 = asyncio.create_task(svc2.run())
            await asyncio.sleep(0.10)  # allow lease election

            def _submit_duped_batch(start_id: int, n: int) -> None:
                for i in range(n):
                    iid = start_id + i
                    key = f"ha-{iid}"
                    ch1.submit_nowait(_make_intent(iid, key))
                    ch2.submit_nowait(_make_intent(iid, key))

            _submit_duped_batch(1, 50)
            await asyncio.sleep(0.30)

            # Only one gateway should have dispatched each unique intent.
            assert shared_api_queue.qsize() == 50

            leader_first = svc1 if svc1.get_health().get("leader_active") else svc2
            leader_task = t1 if leader_first is svc1 else t2

            # Simulate gateway outage (leader dies).
            leader_task.cancel()
            with suppress(asyncio.CancelledError):
                await leader_task
            await asyncio.sleep(0.20)  # standby should acquire lease

            _submit_duped_batch(51, 50)
            await asyncio.sleep(0.40)

            for task in (t1, t2):
                if task.done():
                    continue
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

            cmds = []
            while not shared_api_queue.empty():
                cmds.append(shared_api_queue.get_nowait())
            assert len(cmds) == 100
            cmd_ids = [int(cmd.cmd_id) for cmd in cmds]
            assert len(set(cmd_ids)) == 100
            assert min(cmd_ids) == 1 and max(cmd_ids) == 100
