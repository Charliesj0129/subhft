"""A TrackGate drop must leave a counter behind, not only a debug log.

On 2026-08-10 R47 emitted 2,703 intents on TMFH6 and every one was discarded
by the session-phase filter, which runs at ``runner.py`` *before* the
intent/flat classification. The only trace was ``logger.debug``, so the runner
recorded all of them as ``flat`` — the same value a strategy that simply chose
not to quote would produce. Two months of silence looked identical to two
months of "no signal today".

These tests pin the counter that makes the two distinguishable.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import Counter

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.session_governor import SessionPhase, TrackGate


def _make_bus():
    bus = MagicMock()

    async def _gen():
        return
        yield  # pragma: no cover - generator protocol

    bus.consume.return_value = _gen()
    return bus


class _QuotingStrategy:
    """Emits one NEW intent for *intent_symbol* on every event it receives."""

    def __init__(self, sid: str, event_symbol: str, intent_symbol: str) -> None:
        self.strategy_id = sid
        self.symbols = {event_symbol}
        self.enabled = True
        self.required_features: list[str] = []
        self.required_feature_profile = None
        self._intent_symbol = intent_symbol
        self.feedback: list = []

    def handle_event(self, ctx, event):  # noqa: ARG002 - runner contract
        return [
            SimpleNamespace(
                intent_id="i-1",
                strategy_id=self.strategy_id,
                symbol=self._intent_symbol,
                intent_type=1,  # NEW
                side=1,
                tif=0,
                session_phase=None,
            )
        ]

    def on_risk_feedback(self, fb) -> None:
        self.feedback.append(fb)


@pytest.fixture()
def _runner_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_STRATEGY_CONFIG", str(tmp_path / "empty.yaml"))
    (tmp_path / "empty.yaml").write_text("strategies: []\n")
    monkeypatch.setenv("HFT_STRATEGY_CIRCUIT_RUST", "0")
    monkeypatch.setenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "0")


def _build_runner(metrics):
    from hft_platform.strategy.runner import StrategyRunner

    with patch("hft_platform.strategy.runner.MetricsRegistry") as mr:
        mr.get.return_value = metrics
        with patch("hft_platform.strategy.runner.LatencyRecorder") as lr:
            lr.get.return_value = MagicMock()
            runner = StrategyRunner(bus=_make_bus(), risk_queue=MagicMock(spec=["put_nowait"]))
    runner._typed_intent_fastpath = False
    return runner


@pytest.mark.unit
def test_metric_exists_with_strategy_symbol_and_phase_labels() -> None:
    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()

    assert isinstance(registry.track_gate_intents_filtered_total, Counter)
    assert registry.track_gate_intents_filtered_total._labelnames == ("strategy", "symbol", "phase")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_filtered_intent_increments_the_drop_counter(_runner_env) -> None:
    """The exact shape of the outage: the event symbol is in-track, the intent's is not."""
    metrics = MagicMock()
    metrics.cap_symbol.side_effect = lambda s: s
    runner = _build_runner(metrics)

    gate = TrackGate()
    gate.register_symbol("TMFE6", "futures_day")  # the stale month, as shipped
    gate.set_track_phase("futures_day", SessionPhase.OPEN)
    runner.track_gate = gate

    runner.register(_QuotingStrategy("R47_MAKER_TMF", event_symbol="TMFH6", intent_symbol="TMFH6"))
    await runner.process_event(SimpleNamespace(symbol="TMFH6", ts=0))

    metrics.track_gate_intents_filtered_total.labels.assert_called_once_with(
        strategy="R47_MAKER_TMF",
        symbol="TMFH6",
        phase="CLOSED",
    )
    metrics.track_gate_intents_filtered_total.labels.return_value.inc.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intent_that_passes_the_gate_does_not_increment_the_counter(_runner_env) -> None:
    metrics = MagicMock()
    metrics.cap_symbol.side_effect = lambda s: s
    runner = _build_runner(metrics)

    gate = TrackGate()
    gate.register_root("TMF", "futures_day")
    gate.set_track_phase("futures_day", SessionPhase.OPEN)
    runner.track_gate = gate

    runner.register(_QuotingStrategy("R47_MAKER_TMF", event_symbol="TMFH6", intent_symbol="TMFH6"))
    await runner.process_event(SimpleNamespace(symbol="TMFH6", ts=0))

    metrics.track_gate_intents_filtered_total.labels.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drop_counter_records_the_blocking_phase(_runner_env) -> None:
    """CLOSE_ONLY blocking a NEW must be distinguishable from an unknown symbol."""
    metrics = MagicMock()
    metrics.cap_symbol.side_effect = lambda s: s
    runner = _build_runner(metrics)

    gate = TrackGate()
    gate.register_root("TMF", "futures_day")
    gate.set_track_phase("futures_day", SessionPhase.CLOSE_ONLY)
    runner.track_gate = gate

    runner.register(_QuotingStrategy("R47_MAKER_TMF", event_symbol="TMFH6", intent_symbol="TMFH6"))
    await runner.process_event(SimpleNamespace(symbol="TMFH6", ts=0))

    metrics.track_gate_intents_filtered_total.labels.assert_called_once_with(
        strategy="R47_MAKER_TMF",
        symbol="TMFH6",
        phase="CLOSE_ONLY",
    )
