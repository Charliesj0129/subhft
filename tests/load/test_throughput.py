"""WU-12: Throughput test for GatewayService pipeline.

Pushes 10k OrderIntents through the gateway with mocked adapter/risk,
asserts >5k/sec throughput.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hft_platform.gateway.channel import LocalIntentChannel
from hft_platform.gateway.service import GatewayService

from .conftest import MockOrderAdapter, MockRiskEngine, make_intent


@pytest.mark.load
class TestGatewayThroughput:
    """Throughput benchmark for the GatewayService hot path."""

    INTENT_COUNT = 10_000
    MIN_THROUGHPUT = 5_000  # intents/sec

    @pytest.mark.asyncio
    async def test_throughput_exceeds_5k_per_sec(
        self,
        gateway_service: GatewayService,
        channel: LocalIntentChannel,
        mock_adapter: MockOrderAdapter,
        mock_risk_engine: MockRiskEngine,
    ) -> None:
        """Push 10k intents and verify >5k/sec throughput."""
        # Pre-generate intents to avoid timing allocation overhead
        intents = [make_intent(i) for i in range(self.INTENT_COUNT)]

        # Submit all intents to the channel
        for intent in intents:
            channel.submit_nowait(intent)

        assert channel.qsize() == self.INTENT_COUNT

        # Process all envelopes through the gateway
        t0 = time.perf_counter()
        processed = 0
        for _ in range(self.INTENT_COUNT):
            envelope = await channel.receive()
            await gateway_service._process_envelope(envelope)
            channel.task_done()
            processed += 1
        elapsed = time.perf_counter() - t0

        throughput = processed / elapsed if elapsed > 0 else float("inf")

        # Assertions
        assert processed == self.INTENT_COUNT
        assert mock_risk_engine.evaluate_count == self.INTENT_COUNT
        assert mock_adapter._api_queue.qsize() == self.INTENT_COUNT
        assert throughput >= self.MIN_THROUGHPUT, (
            f"Throughput {throughput:.0f}/sec below minimum {self.MIN_THROUGHPUT}/sec "
            f"({processed} intents in {elapsed:.3f}s)"
        )

    @pytest.mark.asyncio
    async def test_throughput_with_dedup_misses(
        self,
        gateway_service: GatewayService,
        channel: LocalIntentChannel,
        mock_adapter: MockOrderAdapter,
    ) -> None:
        """Verify throughput with unique idempotency keys (all dedup misses)."""
        count = 5_000
        for i in range(count):
            channel.submit_nowait(make_intent(i, idempotency_key=f"unique-{i}"))

        t0 = time.perf_counter()
        for _ in range(count):
            envelope = await channel.receive()
            await gateway_service._process_envelope(envelope)
            channel.task_done()
        elapsed = time.perf_counter() - t0

        throughput = count / elapsed if elapsed > 0 else float("inf")
        assert throughput >= self.MIN_THROUGHPUT / 2, (
            f"Dedup-miss throughput {throughput:.0f}/sec too low"
        )
        assert mock_adapter._api_queue.qsize() == count
