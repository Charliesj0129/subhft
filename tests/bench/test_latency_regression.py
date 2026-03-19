"""WU-19: Latency regression gate benchmarks.

Measures P99 latency for critical hot-path functions and fails if
thresholds (from thresholds.yaml) are exceeded.

Benchmarks (1k iterations each):
- RiskEngine.evaluate() < 50us P99
- GatewayService._process_envelope() < 100us P99
- normalize_tick() < 10us P99
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF
from hft_platform.core import timebase
from hft_platform.gateway.service import GatewayService
from hft_platform.risk.engine import RiskEngine

from .conftest import make_bench_envelope, make_bench_intent

# Load thresholds
_THRESHOLDS_PATH = Path(__file__).parent / "thresholds.yaml"
with open(_THRESHOLDS_PATH) as _f:
    _THRESHOLDS = yaml.safe_load(_f)

_ITERATIONS = int(_THRESHOLDS.get("iterations", 1000))
_RISK_P99_US = float(_THRESHOLDS["risk_evaluate_p99_us"])
_GATEWAY_P99_US = float(_THRESHOLDS["gateway_process_envelope_p99_us"])
_NORMALIZE_P99_US = float(_THRESHOLDS["normalize_tick_p99_us"])


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute percentile from a pre-sorted list."""
    if not sorted_values:
        return 0.0
    idx = int(len(sorted_values) * pct / 100.0)
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


def _measure_ns(fn: Any, iterations: int) -> list[int]:
    """Call fn() `iterations` times, return list of durations in nanoseconds."""
    durations: list[int] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        fn()
        durations.append(time.perf_counter_ns() - t0)
    return durations


@pytest.mark.bench
class TestRiskEvaluateLatency:
    """RiskEngine.evaluate() P99 must be < 50us."""

    def test_evaluate_p99_under_threshold(self, risk_engine: RiskEngine) -> None:
        intent = make_bench_intent()

        # Warm up
        for _ in range(100):
            risk_engine.evaluate(intent)

        # Measure
        durations_ns = _measure_ns(lambda: risk_engine.evaluate(intent), _ITERATIONS)
        durations_us = sorted(d / 1000.0 for d in durations_ns)
        p99 = _percentile(durations_us, 99)

        assert p99 < _RISK_P99_US, (
            f"RiskEngine.evaluate() P99={p99:.1f}us exceeds threshold {_RISK_P99_US}us"
        )

    def test_evaluate_cancel_fast_path(self, risk_engine: RiskEngine) -> None:
        """CANCEL intents should be even faster (skip validators)."""
        cancel_intent = OrderIntent(
            intent_id=999,
            strategy_id="bench_strat",
            symbol="2330",
            intent_type=IntentType.CANCEL,
            side=Side.BUY,
            price=0,
            qty=0,
            tif=TIF.LIMIT,
            timestamp_ns=timebase.now_ns(),
            target_order_id="order-1",
        )

        for _ in range(50):
            risk_engine.evaluate(cancel_intent)

        durations_ns = _measure_ns(lambda: risk_engine.evaluate(cancel_intent), _ITERATIONS)
        durations_us = sorted(d / 1000.0 for d in durations_ns)
        p99 = _percentile(durations_us, 99)

        # CANCEL should be faster than the general threshold
        assert p99 < _RISK_P99_US, (
            f"RiskEngine.evaluate(CANCEL) P99={p99:.1f}us exceeds threshold {_RISK_P99_US}us"
        )


@pytest.mark.bench
class TestGatewayEnvelopeLatency:
    """GatewayService._process_envelope() P99 must be < 100us."""

    @pytest.mark.asyncio
    async def test_process_envelope_p99_under_threshold(
        self, gateway_for_bench: GatewayService
    ) -> None:
        svc = gateway_for_bench

        # Warm up
        for i in range(100):
            env = make_bench_envelope(intent_id=i)
            await svc._process_envelope(env)

        # Measure
        durations_ns: list[int] = []
        for i in range(_ITERATIONS):
            env = make_bench_envelope(intent_id=10_000 + i)
            t0 = time.perf_counter_ns()
            await svc._process_envelope(env)
            durations_ns.append(time.perf_counter_ns() - t0)

        durations_us = sorted(d / 1000.0 for d in durations_ns)
        p99 = _percentile(durations_us, 99)

        assert p99 < _GATEWAY_P99_US, (
            f"GatewayService._process_envelope() P99={p99:.1f}us exceeds threshold {_GATEWAY_P99_US}us"
        )


@pytest.mark.bench
class TestNormalizeTickLatency:
    """normalize_tick() P99 must be < 10us."""

    def test_normalize_tick_p99_under_threshold(self) -> None:
        """Benchmark the normalizer's tick normalization path."""
        from hft_platform.feed_adapter.normalizer import Normalizer

        normalizer = Normalizer()

        # Simulate a Shioaji-like tick payload
        payload = {
            "code": "2330",
            "close": 500.0,
            "volume": 100,
            "ts": 1700000000000000000,
            "total_volume": 10000,
            "simtrade": 0,
            "intraday_odd": 0,
        }

        # Warm up
        for _ in range(100):
            normalizer.normalize_tick(payload)

        # Measure
        durations_ns = _measure_ns(lambda: normalizer.normalize_tick(payload), _ITERATIONS)
        durations_us = sorted(d / 1000.0 for d in durations_ns)
        p99 = _percentile(durations_us, 99)

        assert p99 < _NORMALIZE_P99_US, (
            f"normalize_tick() P99={p99:.1f}us exceeds threshold {_NORMALIZE_P99_US}us"
        )
