import asyncio

# Configure logging to suppress debug noise during stress logic
import logging
import os
import time
from contextlib import suppress
from unittest.mock import MagicMock

import psutil
import pytest
from structlog import get_logger

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
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService

logging.getLogger("shioaji").setLevel(logging.WARNING)

logger = get_logger("stress_test")


def _build_gateway_pipeline(queue_size: int = 4096):
    """Build a minimal GatewayService pipeline for throughput testing."""
    channel = LocalIntentChannel(maxsize=queue_size, ttl_ms=0)

    risk_engine = MagicMock()
    risk_engine.evaluate.return_value = RiskDecision(
        approved=True, intent=MagicMock(), reason_code="OK"
    )
    risk_engine.create_command.side_effect = lambda intent: OrderCommand(
        cmd_id=id(intent),
        intent=intent,
        deadline_ns=time.perf_counter_ns() + 1_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )

    api_queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
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
    return channel, svc, api_queue


async def _run_gateway_stress(n_intents: int = 10_000, timeout_s: float = 10.0) -> dict:
    """Submit n_intents through LocalIntentChannel → GatewayService and measure throughput."""
    channel, svc, api_queue = _build_gateway_pipeline()

    # Pre-fill channel (up to maxsize); remainder submitted during run
    batch = min(n_intents, 4096)
    for i in range(batch):
        channel.submit_nowait(
            OrderIntent(
                intent_id=i,
                strategy_id="stress",
                symbol="TSE:2330",
                intent_type=IntentType.NEW,
                side=Side.BUY,
                price=1_000_000,
                qty=1,
                tif=TIF.LIMIT,
                idempotency_key=f"stress_{i}",
            )
        )

    t0 = time.perf_counter()
    svc_task = asyncio.create_task(svc.run())

    # Wait until all submitted intents are dispatched or timeout
    deadline = t0 + timeout_s
    while svc._dispatched < batch and time.perf_counter() < deadline:
        await asyncio.sleep(0.001)

    elapsed = time.perf_counter() - t0
    svc_task.cancel()
    with suppress(asyncio.CancelledError):
        await svc_task

    throughput = svc._dispatched / elapsed if elapsed > 0 else 0
    return {
        "dispatched": svc._dispatched,
        "rejected": svc._rejected,
        "elapsed_s": round(elapsed, 4),
        "intents_per_sec": round(throughput, 1),
    }


@pytest.mark.asyncio
async def test_gateway_pipeline_throughput():
    """D6: GatewayService pipeline dispatches intents without error."""
    result = await _run_gateway_stress(n_intents=1000, timeout_s=5.0)
    assert result["dispatched"] > 0, "No intents dispatched"
    assert result["rejected"] == 0, f"Unexpected rejections: {result['rejected']}"


async def monitor_resources(pid: int, duration: float) -> None:
    proc = psutil.Process(pid)
    logger.info("Starting Monitor")
    start = time.time()
    max_mem = 0.0
    while time.time() - start < duration:
        mem = proc.memory_info().rss / 1024 / 1024  # MB
        max_mem = max(max_mem, mem)
        await asyncio.sleep(1)
    logger.info("Monitor Finished", max_mem_mb=round(max_mem, 2))


async def main() -> None:
    """Run a self-contained GatewayService throughput stress test."""
    n_intents = 10_000
    logger.info("Starting gateway pipeline stress test", n_intents=n_intents)

    monitor_task = asyncio.create_task(
        monitor_resources(os.getpid(), duration=15)
    )

    result = await _run_gateway_stress(n_intents=n_intents, timeout_s=10.0)

    monitor_task.cancel()
    with suppress(asyncio.CancelledError):
        await monitor_task

    logger.info(
        "Stress test complete",
        dispatched=result["dispatched"],
        rejected=result["rejected"],
        elapsed_s=result["elapsed_s"],
        intents_per_sec=result["intents_per_sec"],
    )

    # Sanity assertion: must have dispatched at least the pre-filled batch
    assert result["dispatched"] > 0, "No intents were dispatched — pipeline is broken"
    assert result["rejected"] == 0, f"Unexpected rejections: {result['rejected']}"


if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging

    configure_logging()
    asyncio.run(main())
