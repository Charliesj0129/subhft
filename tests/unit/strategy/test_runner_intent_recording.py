"""Slice C task 3 — StrategyRunner producer hook for the opt-in
``intents`` recorder topic.

Three behaviors:
1. With ``HFT_INTENT_RECORDER_ENABLED`` unset, no recorder envelope is enqueued
   even when an intent is submitted to risk.
2. With the env var set to ``"1"``, a successful submit puts a single
   ``{"topic": "intents", "data": {...}}`` envelope on ``recorder_queue``.
3. When the recorder queue is full, the producer hook silently drops via
   ``put_nowait`` + ``asyncio.QueueFull`` and bumps
   ``metrics.recorder_intent_drop_total`` — the strategy MUST never block.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side

# ---------------------------------------------------------------------------
# Helpers / stubs (mirror tests/unit/test_runner_queue_full.py)
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
    def __init__(self, sid: str = "strat_a", symbols=None):
        self.strategy_id = sid
        self.symbols = set(symbols) if symbols else {"TMFD6"}
        self.enabled = True
        self.required_features = []
        self.required_feature_profile = None
        self._calls: list = []
        self._return_value: list = []

    def handle_event(self, ctx, event):
        self._calls.append((ctx, event))
        return self._return_value


def _make_intent(strategy_id: str = "strat_a", intent_id: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol="TMFD6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=171_950_000,
        qty=1,
        tif=TIF.LIMIT,
        timestamp_ns=0,
        source_ts_ns=0,
        decision_price=171_945_000,
        price_type="LMT",
    )


def _make_event(symbol: str = "TMFD6", ts: int = 0):
    # ts=0 triggers fallback to now_ns() in _extract_event_trace so events are
    # always considered fresh by the runner's stale-event guard.
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
    # Disable the typed-intent fastpath so we drive the standard
    # ``self._risk_submit(intent)`` branch with our MagicMock submit hook.
    monkeypatch.setenv("HFT_TYPED_INTENT_CHANNEL", "0")


@pytest.fixture()
def mock_metrics():
    m = MagicMock()
    m.strategy_latency_ns.labels.return_value = MagicMock()
    m.strategy_intents_total.labels.return_value = MagicMock()
    m.feature_profile_compat_failures_total = MagicMock()
    m.strategy_timeout_total.labels.return_value = MagicMock()
    m.strategy_circuit_break_total.labels.return_value = MagicMock()
    m.intent_queue_full_total = MagicMock()
    m.recorder_intent_drop_total = MagicMock()
    return m


@pytest.fixture()
def make_runner(mock_metrics):
    def _factory(risk_queue=None, recorder_queue=None):
        with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
            mr.get.return_value = mock_metrics
            with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
                lr.get.return_value = MagicMock()
                from hft_platform.strategy.runner import StrategyRunner

                bus = _make_bus()
                rq = risk_queue if risk_queue is not None else MagicMock(spec=["put_nowait"])
                runner = StrategyRunner(bus, rq, recorder_queue=recorder_queue)
                return runner

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_does_not_record_intents_when_disabled(make_runner, monkeypatch):
    """With ``HFT_INTENT_RECORDER_ENABLED`` unset, the recorder queue stays empty."""
    monkeypatch.delenv("HFT_INTENT_RECORDER_ENABLED", raising=False)

    rec_q: asyncio.Queue = asyncio.Queue(maxsize=8)
    risk_queue = MagicMock(spec=["put_nowait"])
    runner = make_runner(risk_queue=risk_queue, recorder_queue=rec_q)

    # Sanity: the runner must have cached the disabled flag.
    assert runner._intent_recorder_enabled is False

    strat = _FakeStrategy(sid="r47_disabled")
    strat._return_value = [_make_intent("r47_disabled", intent_id=11)]
    runner.register(strat)

    await runner.process_event(_make_event())

    risk_queue.put_nowait.assert_called_once()
    assert rec_q.qsize() == 0, "recorder queue must remain empty when env var unset"


@pytest.mark.asyncio
async def test_runner_records_intent_after_successful_submit(make_runner, monkeypatch):
    """With env var = '1', exactly one envelope is enqueued per accepted intent."""
    monkeypatch.setenv("HFT_INTENT_RECORDER_ENABLED", "1")

    rec_q: asyncio.Queue = asyncio.Queue(maxsize=8)
    risk_queue = MagicMock(spec=["put_nowait"])
    runner = make_runner(risk_queue=risk_queue, recorder_queue=rec_q)

    assert runner._intent_recorder_enabled is True
    assert runner._recorder_queue is rec_q

    intent = _make_intent("r47_enabled", intent_id=4242)
    strat = _FakeStrategy(sid="r47_enabled")
    strat._return_value = [intent]
    runner.register(strat)

    await runner.process_event(_make_event())

    assert risk_queue.put_nowait.call_count == 1
    assert rec_q.qsize() == 1
    envelope = rec_q.get_nowait()
    assert envelope["topic"] == "intents"
    data = envelope["data"]
    assert data["intent"] is intent  # carry-by-reference; Batcher serializes later
    assert isinstance(data["ingest_ts"], int)
    assert data["ingest_ts"] > 0


@pytest.mark.asyncio
async def test_runner_silent_drop_on_recorder_queue_full(make_runner, mock_metrics, monkeypatch):
    """A full recorder queue MUST silently drop and bump the metric."""
    monkeypatch.setenv("HFT_INTENT_RECORDER_ENABLED", "1")

    # Capacity-1 queue, pre-fill it so the runner's put_nowait hits QueueFull.
    rec_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    rec_q.put_nowait("filler")

    risk_queue = MagicMock(spec=["put_nowait"])
    runner = make_runner(risk_queue=risk_queue, recorder_queue=rec_q)

    strat = _FakeStrategy(sid="r47_drop")
    strat._return_value = [_make_intent("r47_drop", intent_id=99)]
    runner.register(strat)

    # Must NOT raise.
    await runner.process_event(_make_event())

    # Risk submit succeeded.
    risk_queue.put_nowait.assert_called_once()
    # Queue still capped at 1 — our envelope was silently dropped.
    assert rec_q.qsize() == 1
    # Drop counter incremented exactly once.
    mock_metrics.recorder_intent_drop_total.inc.assert_called_once()
