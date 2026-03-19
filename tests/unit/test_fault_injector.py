"""Tests for WU-15: FaultInjector."""

from __future__ import annotations

import os

import pytest

from hft_platform.testing.fault_injector import FaultInjector


@pytest.fixture(autouse=True)
def _reset_singleton():
    FaultInjector.reset()
    env_keys = [
        "HFT_MODE",
        "HFT_FAULT_QUEUE_DROP_PCT",
        "HFT_FAULT_LATENCY_MS",
        "HFT_FAULT_BROKER_ERROR_PCT",
        "HFT_FAULT_FEED_GAP_S",
        "HFT_FAULT_SEED",
    ]
    saved = {k: os.environ.get(k) for k in env_keys}
    for k in env_keys:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    FaultInjector.reset()


class TestFaultInjectorLiveBlock:
    def test_raises_in_live_mode(self) -> None:
        os.environ["HFT_MODE"] = "live"
        with pytest.raises(RuntimeError, match="hard-blocked in live mode"):
            FaultInjector()

    def test_singleton_raises_in_live_mode(self) -> None:
        os.environ["HFT_MODE"] = "live"
        with pytest.raises(RuntimeError, match="hard-blocked in live mode"):
            FaultInjector.get()

    def test_allowed_in_sim_mode(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        assert FaultInjector() is not None

    def test_allowed_in_replay_mode(self) -> None:
        os.environ["HFT_MODE"] = "replay"
        assert FaultInjector() is not None


class TestFaultInjectorSingleton:
    def test_get_returns_same_instance(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        assert FaultInjector.get() is FaultInjector.get()

    def test_reset_clears_instance(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        a = FaultInjector.get()
        FaultInjector.reset()
        assert a is not FaultInjector.get()


class TestQueueDrop:
    def test_zero_pct_never_drops(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_QUEUE_DROP_PCT"] = "0"
        fi = FaultInjector()
        assert not any(fi.should_drop_queue() for _ in range(100))

    def test_100_pct_always_drops(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_QUEUE_DROP_PCT"] = "100"
        fi = FaultInjector()
        assert all(fi.should_drop_queue() for _ in range(100))

    def test_partial_drop_rate(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_QUEUE_DROP_PCT"] = "50"
        os.environ["HFT_FAULT_SEED"] = "42"
        fi = FaultInjector()
        results = [fi.should_drop_queue() for _ in range(1000)]
        assert 0.30 < sum(results) / len(results) < 0.70


class TestBrokerError:
    def test_zero_pct_never_errors(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_BROKER_ERROR_PCT"] = "0"
        fi = FaultInjector()
        assert not any(fi.should_simulate_broker_error() for _ in range(100))

    def test_100_pct_always_errors(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_BROKER_ERROR_PCT"] = "100"
        fi = FaultInjector()
        assert all(fi.should_simulate_broker_error() for _ in range(100))


class TestLatencyInjection:
    @pytest.mark.asyncio
    async def test_zero_latency_is_noop(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_LATENCY_MS"] = "0"
        await FaultInjector().inject_latency()

    @pytest.mark.asyncio
    async def test_nonzero_latency_sleeps(self) -> None:
        import time

        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_LATENCY_MS"] = "50"
        fi = FaultInjector()
        start = time.monotonic()
        await fi.inject_latency()
        assert time.monotonic() - start >= 0.04


class TestFeedGap:
    @pytest.mark.asyncio
    async def test_zero_gap_is_noop(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_FEED_GAP_S"] = "0"
        await FaultInjector().inject_feed_gap()

    @pytest.mark.asyncio
    async def test_nonzero_gap_sleeps(self) -> None:
        import time

        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_FEED_GAP_S"] = "0.05"
        fi = FaultInjector()
        start = time.monotonic()
        await fi.inject_feed_gap()
        assert time.monotonic() - start >= 0.04


class TestSeedReproducibility:
    def test_same_seed_same_results(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_QUEUE_DROP_PCT"] = "50"
        os.environ["HFT_FAULT_SEED"] = "999"
        fi1 = FaultInjector()
        results1 = [fi1.should_drop_queue() for _ in range(20)]
        FaultInjector.reset()
        fi2 = FaultInjector()
        assert results1 == [fi2.should_drop_queue() for _ in range(20)]


class TestProperties:
    def test_properties_reflect_env(self) -> None:
        os.environ["HFT_MODE"] = "sim"
        os.environ["HFT_FAULT_QUEUE_DROP_PCT"] = "10"
        os.environ["HFT_FAULT_LATENCY_MS"] = "5"
        os.environ["HFT_FAULT_BROKER_ERROR_PCT"] = "3"
        os.environ["HFT_FAULT_FEED_GAP_S"] = "0.5"
        fi = FaultInjector()
        assert fi.queue_drop_pct == 10.0
        assert fi.latency_ms == 5.0
        assert fi.broker_error_pct == 3.0
        assert fi.feed_gap_s == 0.5
