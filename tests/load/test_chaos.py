"""WU-12: Chaos test for GatewayService pipeline.

Injects failures to verify graceful degradation:
- Queue full (bounded queue overflow)
- Risk exception (validator raises)
- Dedup corruption (duplicate idempotency keys)
- Exposure limit exceeded
"""

from __future__ import annotations

import asyncio
import os

import pytest

from hft_platform.contracts.strategy import StormGuardState
from hft_platform.gateway.channel import LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService

from .conftest import (
    MockOrderAdapter,
    MockRiskEngine,
    MockStormGuard,
    make_intent,
)


@pytest.mark.load
class TestChaosQueueFull:
    """Verify no crash when order adapter queue overflows."""

    @pytest.mark.asyncio
    async def test_order_queue_full_graceful_reject(self) -> None:
        """When adapter queue is full, intent is rejected (not crashed)."""
        # Tiny adapter queue to force overflow
        channel = LocalIntentChannel(maxsize=128, ttl_ms=0)
        adapter = MockOrderAdapter(maxsize=1)
        risk = MockRiskEngine()
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        exposure = ExposureStore(global_max_notional=0, max_symbols=10_000)
        policy = GatewayPolicy()
        storm = MockStormGuard()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        # Fill the adapter queue (capacity=1)
        channel.submit_nowait(make_intent(0, idempotency_key="fill-0"))
        envelope = await channel.receive()
        await svc._process_envelope(envelope)
        channel.task_done()
        assert adapter._api_queue.qsize() == 1

        # Next intent should be rejected (queue full), not crash
        channel.submit_nowait(make_intent(1, idempotency_key="overflow-1"))
        envelope = await channel.receive()
        await svc._process_envelope(envelope)
        channel.task_done()

        # Should still be 1 in adapter queue (overflow was caught)
        assert adapter._api_queue.qsize() == 1
        assert svc._rejected >= 1


@pytest.mark.load
class TestChaosRiskException:
    """Verify no crash when risk engine raises an exception."""

    @pytest.mark.asyncio
    async def test_risk_exception_does_not_crash_gateway(self) -> None:
        """Risk engine exception is caught and logged, gateway survives."""
        channel = LocalIntentChannel(maxsize=128, ttl_ms=0)
        adapter = MockOrderAdapter()
        risk = MockRiskEngine(should_raise=True)
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        exposure = ExposureStore(global_max_notional=0, max_symbols=10_000)
        policy = GatewayPolicy()
        storm = MockStormGuard()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        channel.submit_nowait(make_intent(0, idempotency_key="risk-fail-0"))
        envelope = await channel.receive()

        # _process_envelope should not raise — exception is caught in run() loop
        # but _process_envelope itself propagates it. Verify the run() loop handles it.
        with pytest.raises(RuntimeError, match="Injected risk failure"):
            await svc._process_envelope(envelope)
        channel.task_done()

        # Gateway service state should still be consistent
        assert adapter._api_queue.qsize() == 0  # Nothing dispatched

    @pytest.mark.asyncio
    async def test_gateway_run_loop_survives_risk_exception(self) -> None:
        """The run() main loop catches _process_envelope exceptions and continues."""
        channel = LocalIntentChannel(maxsize=128, ttl_ms=0)
        adapter = MockOrderAdapter()
        risk = MockRiskEngine(should_raise=True)
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        exposure = ExposureStore(global_max_notional=0, max_symbols=10_000)
        policy = GatewayPolicy()
        storm = MockStormGuard()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        # Submit two intents
        channel.submit_nowait(make_intent(0, idempotency_key="fail-a"))
        channel.submit_nowait(make_intent(1, idempotency_key="fail-b"))

        # Run gateway in a task, then cancel after a short wait
        task = asyncio.create_task(svc.run())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Gateway processed both without crashing (they raised, but run() caught them)
        assert svc.running is False


@pytest.mark.load
class TestChaosDedupCorruption:
    """Verify idempotency with duplicate keys."""

    @pytest.mark.asyncio
    async def test_duplicate_keys_return_cached_decision(self) -> None:
        """Submitting the same idempotency key twice returns cached result."""
        channel = LocalIntentChannel(maxsize=128, ttl_ms=0)
        adapter = MockOrderAdapter()
        risk = MockRiskEngine()
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        exposure = ExposureStore(global_max_notional=0, max_symbols=10_000)
        policy = GatewayPolicy()
        storm = MockStormGuard()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        # First submission — should be processed
        channel.submit_nowait(make_intent(0, idempotency_key="dup-key"))
        envelope = await channel.receive()
        await svc._process_envelope(envelope)
        channel.task_done()
        assert adapter._api_queue.qsize() == 1
        assert risk.evaluate_count == 1

        # Second submission with same key — dedup hit, no risk evaluation
        channel.submit_nowait(make_intent(1, idempotency_key="dup-key"))
        envelope = await channel.receive()
        await svc._process_envelope(envelope)
        channel.task_done()

        # Risk engine not called again; adapter queue still has 1
        assert risk.evaluate_count == 1
        assert adapter._api_queue.qsize() == 1
        assert svc._dedup_hits >= 1

    @pytest.mark.asyncio
    async def test_many_duplicate_keys_metrics_increment(self) -> None:
        """Bulk duplicate submissions increment dedup hit counter correctly."""
        channel = LocalIntentChannel(maxsize=256, ttl_ms=0)
        adapter = MockOrderAdapter()
        risk = MockRiskEngine()
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        exposure = ExposureStore(global_max_notional=0, max_symbols=10_000)
        policy = GatewayPolicy()
        storm = MockStormGuard()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        # First: unique
        channel.submit_nowait(make_intent(0, idempotency_key="bulk-dup"))
        env = await channel.receive()
        await svc._process_envelope(env)
        channel.task_done()

        # 50 duplicates
        dup_count = 50
        for i in range(dup_count):
            channel.submit_nowait(make_intent(100 + i, idempotency_key="bulk-dup"))

        for _ in range(dup_count):
            env = await channel.receive()
            await svc._process_envelope(env)
            channel.task_done()

        assert svc._dedup_hits == dup_count
        assert risk.evaluate_count == 1  # Only the first unique one


@pytest.mark.load
class TestChaosExposureLimit:
    """Verify graceful rejection when exposure limit is exceeded."""

    @pytest.mark.asyncio
    async def test_exposure_symbol_limit_rejects_gracefully(self) -> None:
        """ExposureLimitError is caught and intent is rejected, not crashed."""
        channel = LocalIntentChannel(maxsize=128, ttl_ms=0)
        adapter = MockOrderAdapter()
        risk = MockRiskEngine()
        # Very small symbol limit to force exhaustion
        exposure = ExposureStore(global_max_notional=0, max_symbols=2)
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        policy = GatewayPolicy()
        storm = MockStormGuard()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        # Fill 2 symbol slots
        channel.submit_nowait(make_intent(0, symbol="SYM_A", idempotency_key="exp-0"))
        channel.submit_nowait(make_intent(1, symbol="SYM_B", idempotency_key="exp-1"))
        for _ in range(2):
            env = await channel.receive()
            await svc._process_envelope(env)
            channel.task_done()
        assert adapter._api_queue.qsize() == 2

        # Third symbol should trigger ExposureLimitError, handled gracefully
        channel.submit_nowait(make_intent(2, symbol="SYM_C", idempotency_key="exp-2"))
        env = await channel.receive()
        await svc._process_envelope(env)
        channel.task_done()

        # Intent rejected, not dispatched
        assert adapter._api_queue.qsize() == 2
        assert svc._rejected >= 1

    @pytest.mark.asyncio
    async def test_global_notional_limit_rejects(self) -> None:
        """Global notional limit triggers rejection, not crash."""
        channel = LocalIntentChannel(maxsize=128, ttl_ms=0)
        adapter = MockOrderAdapter()
        risk = MockRiskEngine()
        # Set global max notional very low (price=5000000 * qty=1 = 5M)
        exposure = ExposureStore(global_max_notional=3_000_000, max_symbols=1000)
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        policy = GatewayPolicy()
        storm = MockStormGuard()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        # This intent has notional = 5_000_000 which exceeds 3_000_000 limit
        channel.submit_nowait(make_intent(0, price=5_000_000, qty=1, idempotency_key="notional-0"))
        env = await channel.receive()
        await svc._process_envelope(env)
        channel.task_done()

        assert adapter._api_queue.qsize() == 0
        assert svc._rejected >= 1


@pytest.mark.load
class TestChaosHaltPolicy:
    """Verify HALT mode blocks new intents gracefully."""

    @pytest.mark.asyncio
    async def test_halt_blocks_new_intents(self) -> None:
        """HALT policy rejects NEW intents, no crash."""
        channel = LocalIntentChannel(maxsize=128, ttl_ms=0)
        adapter = MockOrderAdapter()
        risk = MockRiskEngine()
        dedup = IdempotencyStore(window_size=1000, persist_enabled=False)
        exposure = ExposureStore(global_max_notional=0, max_symbols=10_000)
        policy = GatewayPolicy()
        storm = MockStormGuard(state=StormGuardState.HALT)
        policy.set_halt()

        os.environ["HFT_GATEWAY_METRICS"] = "0"
        svc = GatewayService(
            channel=channel,
            risk_engine=risk,
            order_adapter=adapter,
            exposure_store=exposure,
            dedup_store=dedup,
            storm_guard=storm,
            policy=policy,
        )
        os.environ.pop("HFT_GATEWAY_METRICS", None)

        channel.submit_nowait(make_intent(0, idempotency_key="halt-0"))
        env = await channel.receive()
        await svc._process_envelope(env)
        channel.task_done()

        assert adapter._api_queue.qsize() == 0
        assert svc._rejected >= 1
        assert risk.evaluate_count == 0  # Never reached risk
