"""Tests that QueueFull on risk submit does NOT advance the strategy circuit breaker.

Regression guard for: fix(strategy): stop QueueFull from advancing strategy circuit breaker
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs (mirror test_strategy_runner_timeout.py conventions)
# ---------------------------------------------------------------------------


def _make_bus(events=None):
    bus = MagicMock()
    events = events or []

    async def _gen():
        for e in events:
            yield e

    bus.consume.return_value = _gen()
    return bus


class _FakeStrategy:
    def __init__(self, sid="strat_a", symbols=None, enabled=True):
        self.strategy_id = sid
        self.symbols = set(symbols) if symbols else {"TSMC"}
        self.enabled = enabled
        self.required_features = []
        self.required_feature_profile = None
        self._calls: list = []
        self._return_value: list = []

    def handle_event(self, ctx, event):
        self._calls.append((ctx, event))
        return self._return_value


def _make_intent(strategy_id: str = "strat_a"):
    intent = MagicMock()
    intent.strategy_id = strategy_id
    return intent


def _make_event(symbol: str = "TSMC", ts: int = 123_000_000_000):
    return SimpleNamespace(symbol=symbol, ts=ts)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    # Force Python-only circuit breaker for deterministic assertions
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")
    # Low threshold so that if the bug regresses it trips quickly
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "3")


@pytest.fixture()
def mock_metrics():
    m = MagicMock()
    m.strategy_latency_ns.labels.return_value = MagicMock()
    m.strategy_intents_total.labels.return_value = MagicMock()
    m.feature_profile_compat_failures_total = MagicMock()
    m.strategy_timeout_total.labels.return_value = MagicMock()
    m.strategy_circuit_break_total.labels.return_value = MagicMock()
    m.intent_queue_full_total = MagicMock()
    return m


@pytest.fixture()
def make_runner(mock_metrics):
    def _factory(risk_queue=None):
        with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
            mr.get.return_value = mock_metrics
            with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
                lr.get.return_value = MagicMock()
                from hft_platform.strategy.runner import StrategyRunner

                bus = _make_bus()
                rq = risk_queue if risk_queue is not None else MagicMock(spec=["put_nowait"])
                runner = StrategyRunner(bus, rq)
                return runner

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_does_not_advance_circuit_breaker(make_runner, mock_metrics):
    """QueueFull drops must NOT advance the circuit breaker state.

    Scenario:
    - Strategy returns one intent per event
    - The risk queue always raises QueueFull
    - After many events the circuit state must remain 'normal'
    - strategy.enabled must stay True
    """
    # Risk queue that always raises QueueFull
    always_full_queue = MagicMock(spec=["put_nowait"])
    always_full_queue.put_nowait.side_effect = asyncio.QueueFull()

    runner = make_runner(risk_queue=always_full_queue)

    strat = _FakeStrategy(sid="healthy_strat")
    # Each handle_event call returns one intent
    strat._return_value = [_make_intent("healthy_strat")]
    runner.register(strat)

    # Fire more events than the circuit threshold (3) to ensure no escalation
    for _ in range(10):
        await runner.process_event(_make_event())

    # Circuit state must remain normal
    assert runner._circuit_states.get("healthy_strat", "normal") == "normal", (
        "QueueFull should not advance the circuit breaker"
    )
    # Strategy must remain enabled
    assert strat.enabled is True, "QueueFull should not disable the strategy"
    # Strategy must have received all events (it was never broken)
    assert len(strat._calls) == 10, "Strategy should have received all 10 events"


@pytest.mark.asyncio
async def test_queue_full_does_not_increment_failure_counts(make_runner, mock_metrics):
    """failure_counts dict must stay empty when drops are due to QueueFull."""
    always_full_queue = MagicMock(spec=["put_nowait"])
    always_full_queue.put_nowait.side_effect = asyncio.QueueFull()

    runner = make_runner(risk_queue=always_full_queue)

    strat = _FakeStrategy(sid="strat_b")
    strat._return_value = [_make_intent("strat_b")]
    runner.register(strat)

    for _ in range(5):
        await runner.process_event(_make_event())

    assert runner._failure_counts.get("strat_b", 0) == 0, (
        "failure_counts must not be incremented for QueueFull drops"
    )
