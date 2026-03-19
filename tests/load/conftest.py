"""Shared fixtures for load and chaos tests (WU-12).

Provides mock adapter, mock risk engine, and full gateway pipeline setup
for throughput and chaos testing.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from typing import Any

import pytest

from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskDecision,
    Side,
    StormGuardState,
    TIF,
)
from hft_platform.core import timebase
from hft_platform.gateway.channel import LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService


@dataclass(slots=True)
class MockStormGuard:
    """Minimal StormGuard stub for gateway tests."""

    state: StormGuardState = StormGuardState.NORMAL


class MockRiskEngine:
    """Minimal RiskEngine stub that always approves intents."""

    def __init__(self, *, should_raise: bool = False) -> None:
        self._should_raise = should_raise
        self._monotonic_cmd_id = 0
        self.storm_guard = MockStormGuard()
        self.evaluate_count = 0

    def evaluate(self, intent: Any) -> RiskDecision:
        self.evaluate_count += 1
        if self._should_raise:
            raise RuntimeError("Injected risk failure")
        return RiskDecision(approved=True, intent=intent)

    def create_command(self, intent: OrderIntent) -> OrderCommand:
        self._monotonic_cmd_id += 1
        return OrderCommand(
            cmd_id=self._monotonic_cmd_id,
            intent=intent,
            deadline_ns=timebase.now_ns() + 500_000_000,
            storm_guard_state=self.storm_guard.state,
            created_ns=timebase.now_ns(),
        )


class MockOrderAdapter:
    """Minimal OrderAdapter stub with a bounded queue."""

    def __init__(self, maxsize: int = 65536) -> None:
        self._api_queue: asyncio.Queue[OrderCommand] = asyncio.Queue(maxsize=maxsize)
        self.dispatched: list[OrderCommand] = []


def make_intent(
    intent_id: int,
    *,
    strategy_id: str = "s1",
    symbol: str = "2330",
    price: int = 5000000,
    qty: int = 1,
    idempotency_key: str = "",
) -> OrderIntent:
    """Factory for test OrderIntents with sensible defaults."""
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        timestamp_ns=timebase.now_ns(),
        idempotency_key=idempotency_key or f"key-{intent_id}",
    )


@pytest.fixture()
def channel() -> LocalIntentChannel:
    """Bounded intent channel with large capacity for throughput tests."""
    return LocalIntentChannel(maxsize=65536, ttl_ms=0, dlq_maxsize=1000)


@pytest.fixture()
def mock_risk_engine() -> MockRiskEngine:
    return MockRiskEngine()


@pytest.fixture()
def failing_risk_engine() -> MockRiskEngine:
    return MockRiskEngine(should_raise=True)


@pytest.fixture()
def mock_adapter() -> MockOrderAdapter:
    return MockOrderAdapter(maxsize=65536)


@pytest.fixture()
def dedup_store() -> IdempotencyStore:
    return IdempotencyStore(window_size=100_000, persist_enabled=False)


@pytest.fixture()
def exposure_store() -> ExposureStore:
    return ExposureStore(global_max_notional=0, max_symbols=10_000)


@pytest.fixture()
def policy() -> GatewayPolicy:
    return GatewayPolicy()


@pytest.fixture()
def storm_guard() -> MockStormGuard:
    return MockStormGuard()


@pytest.fixture()
def gateway_service(
    channel: LocalIntentChannel,
    mock_risk_engine: MockRiskEngine,
    mock_adapter: MockOrderAdapter,
    exposure_store: ExposureStore,
    dedup_store: IdempotencyStore,
    storm_guard: MockStormGuard,
    policy: GatewayPolicy,
) -> GatewayService:
    """Fully wired GatewayService for load testing."""
    # Disable metrics to avoid Prometheus registry collisions in tests
    os.environ["HFT_GATEWAY_METRICS"] = "0"
    svc = GatewayService(
        channel=channel,
        risk_engine=mock_risk_engine,
        order_adapter=mock_adapter,
        exposure_store=exposure_store,
        dedup_store=dedup_store,
        storm_guard=storm_guard,
        policy=policy,
    )
    os.environ.pop("HFT_GATEWAY_METRICS", None)
    return svc
