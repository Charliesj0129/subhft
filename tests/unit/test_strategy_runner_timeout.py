"""Tests for per-strategy wall-clock timeout circuit breaker in StrategyRunner."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_bus(events=None):
    bus = MagicMock()
    events = events or []

    async def _gen():
        for e in events:
            yield e

    bus.consume.return_value = _gen()
    return bus


def _make_risk_queue():
    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock()
    return rq


class _FakeStrategy:
    def __init__(self, sid="strat_a", symbols=None, enabled=True, delay_ns=0):
        self.strategy_id = sid
        self.symbols = set(symbols) if symbols else {"TSMC"}
        self.enabled = enabled
        self.required_features = []
        self.required_feature_profile = None
        self._calls = []
        self._return_value = []
        self._delay_ns = delay_ns

    def handle_event(self, ctx, event):
        self._calls.append((ctx, event))
        if self._delay_ns > 0:
            # Busy-wait to simulate slow strategy
            end = time.perf_counter_ns() + self._delay_ns
            while time.perf_counter_ns() < end:
                pass
        return self._return_value


class _SlowThenFastStrategy:
    """Strategy that is slow for the first N calls, then fast."""

    def __init__(self, sid="strat_slow", symbols=None, slow_count=3, delay_ns=0):
        self.strategy_id = sid
        self.symbols = set(symbols) if symbols else {"TSMC"}
        self.enabled = True
        self.required_features = []
        self.required_feature_profile = None
        self._calls = []
        self._return_value = []
        self._slow_count = slow_count
        self._delay_ns = delay_ns

    def handle_event(self, ctx, event):
        self._calls.append((ctx, event))
        if len(self._calls) <= self._slow_count and self._delay_ns > 0:
            end = time.perf_counter_ns() + self._delay_ns
            while time.perf_counter_ns() < end:
                pass
        return self._return_value


def _make_event(symbol="TSMC", ts=0):
    # ts=0 triggers fallback to now_ns() in _extract_event_trace so events are always fresh
    return SimpleNamespace(symbol=symbol, ts=ts)


# Disable strategy registry auto-load and use Python circuit breaker
@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")
    # Set a very short timeout for testing (1ms)
    monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "1")
    monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_STRIKES", "3")
    monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_RECOVER_S", "0.1")  # 100ms for tests


@pytest.fixture()
def mock_metrics():
    m = MagicMock()
    m.strategy_latency_ns.labels.return_value = MagicMock()
    m.strategy_intents_total.labels.return_value = MagicMock()
    m.feature_profile_compat_failures_total = MagicMock()
    m.strategy_timeout_total.labels.return_value = MagicMock()
    m.strategy_circuit_break_total.labels.return_value = MagicMock()
    return m


@pytest.fixture()
def make_runner(mock_metrics):
    def _factory():
        with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
            mr.get.return_value = mock_metrics
            with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
                lr.get.return_value = MagicMock()
                from hft_platform.strategy.runner import StrategyRunner

                bus = _make_bus()
                rq = _make_risk_queue()
                runner = StrategyRunner(bus, rq)
                return runner

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_strategy_gets_circuit_broken(make_runner, mock_metrics):
    """A strategy exceeding timeout for N consecutive strikes gets circuit-broken."""
    runner = make_runner()
    # 5ms busy-wait, timeout is 1ms, strikes=3
    slow_strat = _FakeStrategy(sid="slow_one", delay_ns=5_000_000)
    runner.register(slow_strat)

    # Send 3 events to trigger 3 consecutive timeouts
    for _ in range(3):
        await runner.process_event(_make_event())

    # After 3 strikes, strategy should be timeout-broken
    assert runner._timeout_broken.get("slow_one") is True
    assert runner._timeout_consecutive.get("slow_one", 0) >= 3

    # 4th event should be skipped (strategy is broken)
    call_count_before = len(slow_strat._calls)
    await runner.process_event(_make_event())
    assert len(slow_strat._calls) == call_count_before, "Broken strategy should not receive events"


@pytest.mark.asyncio
async def test_fast_strategy_not_circuit_broken(make_runner):
    """A fast strategy should never be circuit-broken."""
    runner = make_runner()
    fast_strat = _FakeStrategy(sid="fast_one", delay_ns=0)
    runner.register(fast_strat)

    for _ in range(10):
        await runner.process_event(_make_event())

    assert runner._timeout_broken.get("fast_one", False) is False
    assert len(fast_strat._calls) == 10


@pytest.mark.asyncio
async def test_other_strategies_continue_when_one_is_broken(make_runner):
    """When one strategy is broken, others continue receiving events."""
    runner = make_runner()
    slow_strat = _FakeStrategy(sid="slow_one", delay_ns=5_000_000)
    fast_strat = _FakeStrategy(sid="fast_one", delay_ns=0)
    runner.register(slow_strat)
    runner.register(fast_strat)

    # 3 events to break slow_one
    for _ in range(3):
        await runner.process_event(_make_event())

    assert runner._timeout_broken.get("slow_one") is True

    # Send more events - fast_one should still receive them
    fast_calls_before = len(fast_strat._calls)
    for _ in range(5):
        await runner.process_event(_make_event())

    assert len(fast_strat._calls) == fast_calls_before + 5
    # slow_one should NOT have received the extra 5
    assert len(slow_strat._calls) == 3


@pytest.mark.asyncio
async def test_timeout_auto_recovery(make_runner, monkeypatch):
    """Circuit-broken strategy auto-recovers after HFT_STRATEGY_TIMEOUT_RECOVER_S."""
    runner = make_runner()
    slow_strat = _FakeStrategy(sid="slow_one", delay_ns=5_000_000)
    runner.register(slow_strat)

    # Break it
    for _ in range(3):
        await runner.process_event(_make_event())
    assert runner._timeout_broken.get("slow_one") is True

    # Simulate time passing by backdating the broken_at timestamp
    runner._timeout_broken_at_ns["slow_one"] = time.monotonic_ns() - runner._timeout_recover_ns - 1

    # Make strategy fast now so it doesn't re-break
    slow_strat._delay_ns = 0

    # Next event should trigger recovery and process
    calls_before = len(slow_strat._calls)
    await runner.process_event(_make_event())
    assert len(slow_strat._calls) == calls_before + 1
    assert runner._timeout_broken.get("slow_one") is False
    assert runner._timeout_consecutive.get("slow_one") == 0


@pytest.mark.asyncio
async def test_consecutive_counter_resets_on_fast_event(make_runner):
    """A fast event resets the consecutive timeout counter."""
    runner = make_runner()
    strat = _SlowThenFastStrategy(sid="mixed", slow_count=2, delay_ns=5_000_000)
    runner.register(strat)

    # 2 slow events
    await runner.process_event(_make_event())
    await runner.process_event(_make_event())
    assert runner._timeout_consecutive.get("mixed", 0) == 2

    # 1 fast event resets counter
    await runner.process_event(_make_event())
    assert runner._timeout_consecutive.get("mixed", 0) == 0

    # Not broken since we didn't hit 3 consecutive
    assert runner._timeout_broken.get("mixed", False) is False


@pytest.mark.asyncio
async def test_timeout_metrics_incremented(make_runner, mock_metrics):
    """Prometheus metrics are incremented on timeout and circuit break."""
    runner = make_runner()
    slow_strat = _FakeStrategy(sid="slow_one", delay_ns=5_000_000)
    runner.register(slow_strat)

    for _ in range(3):
        await runner.process_event(_make_event())

    # strategy_timeout_total should have been called 3 times
    mock_metrics.strategy_timeout_total.labels.assert_called_with(strategy_name="slow_one")
    timeout_counter = mock_metrics.strategy_timeout_total.labels.return_value
    assert timeout_counter.inc.call_count == 3

    # strategy_circuit_break_total should have been called once
    mock_metrics.strategy_circuit_break_total.labels.assert_called_with(strategy_name="slow_one")
    cb_counter = mock_metrics.strategy_circuit_break_total.labels.return_value
    assert cb_counter.inc.call_count == 1
