"""Shared fixtures for latency regression benchmarks (WU-19).

Provides minimal instances of RiskEngine, GatewayService, and normalizer
for P99 latency measurement.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest
import yaml

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
from hft_platform.gateway.channel import IntentEnvelope, LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService
from hft_platform.risk.engine import RiskEngine


@pytest.fixture()
def risk_config_path(tmp_path: Any) -> str:
    """Create a minimal risk config YAML for RiskEngine instantiation."""
    config = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": 10_000_000,
            "max_qty": 1_000_000,
        },
        "risk": {
            "daily_loss_limit": 10_000_000,
            "max_order_size": 1000,
        },
        "strategies": {},
    }
    config_file = tmp_path / "risk_bench.yaml"
    config_file.write_text(yaml.dump(config))
    return str(config_file)


@pytest.fixture()
def risk_engine(risk_config_path: str) -> RiskEngine:
    """Minimal RiskEngine for benchmarking evaluate()."""
    intent_q: asyncio.Queue[Any] = asyncio.Queue()
    order_q: asyncio.Queue[Any] = asyncio.Queue()
    # Disable optional Rust/FastGate paths for deterministic benchmarking
    os.environ["HFT_RISK_FAST_GATE"] = "0"
    os.environ["HFT_RISK_RUST_VALIDATOR"] = "0"
    engine = RiskEngine(
        config_path=risk_config_path,
        intent_queue=intent_q,
        order_queue=order_q,
    )
    os.environ.pop("HFT_RISK_FAST_GATE", None)
    os.environ.pop("HFT_RISK_RUST_VALIDATOR", None)
    return engine


def make_bench_intent(intent_id: int = 1) -> OrderIntent:
    """Factory for benchmark OrderIntents with realistic fields."""
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="bench_strat",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=5_000_000,  # 500.0 * 10000
        qty=1,
        tif=TIF.LIMIT,
        timestamp_ns=timebase.now_ns(),
        idempotency_key=f"bench-{intent_id}",
    )


def make_bench_envelope(intent_id: int = 1) -> IntentEnvelope:
    """Factory for benchmark IntentEnvelopes."""
    intent = make_bench_intent(intent_id)
    return IntentEnvelope(
        intent=intent,
        enqueued_ns=timebase.now_ns(),
        ack_token=intent.idempotency_key,
    )


class _StubStormGuard:
    state: StormGuardState = StormGuardState.NORMAL


class _StubRiskForGateway:
    """Lightweight risk stub for gateway benchmarks (skip real validators)."""

    def __init__(self) -> None:
        self._cmd_id = 0
        self.storm_guard = _StubStormGuard()

    def evaluate(self, intent: Any) -> RiskDecision:
        return RiskDecision(approved=True, intent=intent)

    def create_command(self, intent: OrderIntent) -> OrderCommand:
        self._cmd_id += 1
        return OrderCommand(
            cmd_id=self._cmd_id,
            intent=intent,
            deadline_ns=timebase.now_ns() + 500_000_000,
            storm_guard_state=StormGuardState.NORMAL,
            created_ns=timebase.now_ns(),
        )


class _StubAdapter:
    def __init__(self) -> None:
        self._api_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=65536)


@pytest.fixture()
def gateway_for_bench() -> GatewayService:
    """GatewayService wired with stubs for envelope processing benchmarks."""
    channel = LocalIntentChannel(maxsize=65536, ttl_ms=0)
    os.environ["HFT_GATEWAY_METRICS"] = "0"
    svc = GatewayService(
        channel=channel,
        risk_engine=_StubRiskForGateway(),
        order_adapter=_StubAdapter(),
        exposure_store=ExposureStore(global_max_notional=0, max_symbols=100_000),
        dedup_store=IdempotencyStore(window_size=100_000, persist_enabled=False),
        storm_guard=_StubStormGuard(),
        policy=GatewayPolicy(),
    )
    os.environ.pop("HFT_GATEWAY_METRICS", None)
    return svc
